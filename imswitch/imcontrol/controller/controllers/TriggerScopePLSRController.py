import os
import configparser
from ast import literal_eval
from ..basecontrollers import ImConWidgetController
import traceback
from imswitch.imcommon.model import APIExport, dirtools
from imswitch.imcontrol.view import guitools


class TriggerScopePLSRController(ImConWidgetController):
    """Linked to TriggerScopePLSRWidget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

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
        self._widget.roScanDeviceEdit.addItems(self.positioners.keys())
        self._widget.cycleScanDeviceEdit.addItems(self.positioners.keys())

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
            self._widget.setOnLaser(self._deviceParameterDict['onLaser'])
            self._widget.setOffLaser(self._deviceParameterDict['offLaser'])
            self._widget.setRoLaser(self._deviceParameterDict['roLaser'])
            self._widget.setRoScanDevice(self._deviceParameterDict['roScanDevice'])
            self._widget.setCycleScanDevice(self._deviceParameterDict['cycleScanDevice'])
        finally:
            self.settingParameters = False

    def getTriggerscopeParameters(self):
        self.getParameters()
        deviceParameterDict = self._deviceParameterDict
        scanParameterDict = {}
        roConvFactor = self.positioners[deviceParameterDict['roScanDevice']].managerProperties['conversionFactor']
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
        return {'deviceParameters': deviceParameterDict, 'scanParameters': scanParameterDict}

    def runScanExternal(self, recalculateSignals, isNonFinalPartOfSequence):
        self._widget.setRepeatEnabled(False)
        self.runScanAdvanced(recalculateSignals=recalculateSignals,
                             isNonFinalPartOfSequence=isNonFinalPartOfSequence,
                             sigScanStartingEmitted=True)

    def runScanAdvanced(self, *, recalculateSignals=True, isNonFinalPartOfSequence=False,
                        sigScanStartingEmitted):
        """Runs a scan with the set scanning parameters."""
        if self._widget.autoStartRec:
            self._commChannel.sigStartRecording.emit()
        try:
            self._widget.setScanButtonChecked(True)
            self.isRunning = True
            self.doingNonFinalPartOfSequence = isNonFinalPartOfSequence
            if not sigScanStartingEmitted:
                self.emitScanSignal(self._commChannel.sigScanStarting)
            triggerscopeParameters = self.getTriggerscopeParameters()
            self._master.scanManager.runScan(triggerscopeParameters, scan_type='pLS-RESOLFTScan')
        except Exception:
            self._logger.error(traceback.format_exc())
            self.isRunning = False

    def abortScan(self):
        self.doingNonFinalPartOfSequence = False
        if not self.isRunning:
            self.scanFailed()

    def scanDone(self):
        self._logger.debug('Scan done')
        self.isRunning = False
        self.emitScanSignal(self._commChannel.sigScanDone)
        if not self.doingNonFinalPartOfSequence:
            self._widget.setScanButtonChecked(False)
            self.emitScanSignal(self._commChannel.sigScanEnded)
        if self._widget.autoStopRec:
            self._commChannel.sigStopRecording.emit()

    def scanFailed(self):
        self._logger.error('Scan failed')
        self.isRunning = False
        self.doingNonFinalPartOfSequence = False
        self._widget.setScanButtonChecked(False)
        self.emitScanSignal(self._commChannel.sigScanEnded)

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
        self._deviceParameterDict['onLaser'] = self._widget.getOnLaser()
        self._deviceParameterDict['offLaser'] = self._widget.getOffLaser()
        self._deviceParameterDict['roLaser'] = self._widget.getRoLaser()
        self._deviceParameterDict['roScanDevice'] = self._widget.getRoScanDevice()
        self._deviceParameterDict['cycleScanDevice'] = self._widget.getCycleScanDevice()

    def emitScanSignal(self, signal, *args):
        signal.emit(*args)

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


_attrCategoryScan = 'MS-RESOLFT_Scan'
_attrCategoryDevices = 'MS-RESOLFT_Dev'
