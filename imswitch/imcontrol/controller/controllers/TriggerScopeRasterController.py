import os
import configparser
from ast import literal_eval
from ..basecontrollers import ImConWidgetController
import numpy as np
import traceback
from imswitch.imcommon.model import APIExport, dirtools
from imswitch.imcontrol.view import guitools
from imswitch.imcommon.view.guitools import colorutils


class TriggerScopeRasterController(ImConWidgetController):
    """Linked to TriggerScopeRasterWidget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.settingAttr = False
        self.settingParameters = False

        self._analogParameterDict = {}
        self._digitalParameterDict = {}
        self.signalDict = None
        self.scanInfoDict = None
        self.isRunning = False
        self.doingNonFinalPartOfSequence = False

        self.positioners = {
            pName: pManager for pName, pManager in self._setupInfo.positioners.items()
            if pManager.forScanning
        }
        self.TTLDevices = self._setupInfo.getTTLDevices()

        self._widget.initControls(
            self.positioners.keys(),
            self.TTLDevices.keys(),
            self._master.scanManager.TTLTimeUnits
        )

        self.scanDir = os.path.join(dirtools.UserFileDirs.Root, 'imcontrol_scans')
        if not os.path.exists(self.scanDir):
            os.makedirs(self.scanDir)

        self.getParameters()
        self.updateSteps()
        self.plotSignalGraph()
        self.updateScanStageAttrs()
        self.updateScanTTLAttrs()

        self._master.scanManager.sigScanStarted.connect(
            lambda: self.emitScanSignal(self._commChannel.sigScanStarted)
        )
        self._master.scanManager.sigScanDone.connect(self.scanDone)

        self._commChannel.sigRunScan.connect(self.runScanExternal)
        self._commChannel.sigAbortScan.connect(self.abortScan)
        self._commChannel.sharedAttrs.sigAttributeSet.connect(self.attrChanged)

        self._widget.sigSaveScanClicked.connect(self.saveScan)
        self._widget.sigLoadScanClicked.connect(self.loadScan)
        self._widget.sigRunScanClicked.connect(self.runScan)
        self._widget.sigSeqTimeParChanged.connect(self.plotSignalGraph)
        self._widget.sigSeqTimeParChanged.connect(self.updateScanTTLAttrs)
        self._widget.sigStageParChanged.connect(self.updateSteps)
        self._widget.sigStageParChanged.connect(self.updateScanStageAttrs)
        self._widget.sigSignalParChanged.connect(self.plotSignalGraph)
        self._widget.sigSignalParChanged.connect(self.updateScanTTLAttrs)

    def saveScan(self):
        fileName = guitools.askForFilePath(self._widget, 'Save scan', self.scanDir, isSaving=True)
        if not fileName:
            return
        self.saveScanParamsToFile(fileName)

    @APIExport(runOnUIThread=True)
    def saveScanParamsToFile(self, filePath: str) -> None:
        """Saves the set scanning parameters to the specified file."""
        self.getParameters()
        config = configparser.ConfigParser()
        config.optionxform = str
        config['analogParameterDict'] = self._analogParameterDict
        config['digitalParameterDict'] = self._digitalParameterDict
        with open(filePath, 'w') as configfile:
            config.write(configfile)

    def loadScan(self):
        fileName = guitools.askForFilePath(self._widget, 'Load scan', self.scanDir)
        if not fileName:
            return
        self.loadScanParamsFromFile(fileName)

    @APIExport(runOnUIThread=True)
    def loadScanParamsFromFile(self, filePath: str) -> None:
        """Loads scanning parameters from the specified file."""
        config = configparser.ConfigParser()
        config.optionxform = str
        config.read(filePath)
        for key in self._analogParameterDict:
            self._analogParameterDict[key] = literal_eval(
                config._sections['analogParameterDict'][key]
            )
        for key in self._digitalParameterDict:
            self._digitalParameterDict[key] = literal_eval(
                config._sections['digitalParameterDict'][key]
            )
        self.setParameters()

    def setParameters(self):
        self.settingParameters = True
        try:
            for i in range(len(self._analogParameterDict['target_device'])):
                positionerName = self._analogParameterDict['target_device'][i]
                self._widget.setScanDim(i, positionerName)
                self._widget.setScanSize(positionerName,
                                         self._analogParameterDict['axis_length'][i])
                self._widget.setScanStepSize(positionerName,
                                             self._analogParameterDict['axis_step_size'][i])

            setTTLDevices = []
            for i in range(len(self._digitalParameterDict['target_device'])):
                deviceName = self._digitalParameterDict['target_device'][i]
                self._widget.setTTLStarts(deviceName, self._digitalParameterDict['TTL_start'][i])
                self._widget.setTTLEnds(deviceName, self._digitalParameterDict['TTL_end'][i])
                setTTLDevices.append(deviceName)

            for deviceName in self.TTLDevices:
                if deviceName not in setTTLDevices:
                    self._widget.unsetTTL(deviceName)

            self._widget.setSeqTimePar(self._digitalParameterDict['sequence_time'])
        finally:
            self.settingParameters = False
            self.plotSignalGraph()

    def getTriggerscopeParameters(self):
        AOtargets = []
        lengthsVolt = []
        stepSizesVolt = []
        startPosVolt = []
        rasterScanParameters = {'Analog': None, 'Digital': None}
        for dim in range(len(self._analogParameterDict['target_device'])):
            target = self._widget.scanPar['scanDim' + str(dim)].currentText()
            convFactor = self.positioners[target].managerProperties['conversionFactor']
            index = self._analogParameterDict['target_device'].index(target)
            AOtargets.append(target)
            lengthsVolt.append(self._analogParameterDict['axis_length'][index] / convFactor)
            stepSizesVolt.append(self._analogParameterDict['axis_step_size'][index] / convFactor)
            startPosVolt.append(self._analogParameterDict['axis_startpos'][index] / convFactor)

        rasterScanParameters['Analog'] = {'targets': AOtargets,
                                          'lengths': lengthsVolt,
                                          'stepSizes': stepSizesVolt,
                                          'startPos': startPosVolt}
        rasterScanParameters['Digital'] = self._digitalParameterDict
        return rasterScanParameters

    def runScanExternal(self, recalculateSignals, isNonFinalPartOfSequence):
        self._widget.setRepeatEnabled(False)
        self.runScanAdvanced(recalculateSignals=recalculateSignals,
                             isNonFinalPartOfSequence=isNonFinalPartOfSequence,
                             sigScanStartingEmitted=True)

    def runScanAdvanced(self, *, recalculateSignals=True, isNonFinalPartOfSequence=False,
                        sigScanStartingEmitted):
        """Runs a scan with the set scanning parameters."""
        try:
            self._widget.setScanButtonChecked(True)
            self.isRunning = True
            self.doingNonFinalPartOfSequence = isNonFinalPartOfSequence
            if not sigScanStartingEmitted:
                self.emitScanSignal(self._commChannel.sigScanStarting)
            # Tell the LaserController which lasers participate in this scan so
            # it can arm them (digital-modulation / external-control mode) and
            # leave the rest off. The TriggerScope firmware runs the scan
            # autonomously, so unlike the Advanced/Nidaq path this signal must
            # be emitted here rather than from the DAQ manager.
            self.emitScanSignal(self._commChannel.sigScanBuilt,
                                self._getScanLaserDevices())
            triggerscopeParameters = self.getTriggerscopeParameters()
            self._master.scanManager.runScan(triggerscopeParameters, scan_type='rasterScan')
        except Exception:
            self._logger.error(traceback.format_exc())
            self.isRunning = False

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
        if not self._widget.repeatEnabled():
            self.emitScanSignal(self._commChannel.sigScanDone)
            if not self.doingNonFinalPartOfSequence:
                self._widget.setScanButtonChecked(False)
                self.emitScanSignal(self._commChannel.sigScanEnded)
        else:
            self._logger.debug('Repeat scan')
            self.runScanAdvanced(sigScanStartingEmitted=True)

    def scanFailed(self):
        self._logger.error('Scan failed')
        self.isRunning = False
        self.doingNonFinalPartOfSequence = False
        self._widget.setScanButtonChecked(False)
        self.emitScanSignal(self._commChannel.sigScanEnded)

    def getParameters(self):
        if self.settingParameters:
            return

        self._analogParameterDict['target_device'] = []
        self._analogParameterDict['axis_length'] = []
        self._analogParameterDict['axis_step_size'] = []
        self._analogParameterDict['axis_startpos'] = []
        for i in range(len(self.positioners)):
            positionerName = self._widget.getScanDim(i)
            size = self._widget.getScanSize(positionerName)
            stepSize = self._widget.getScanStepSize(positionerName)
            start = list(self._master.positionersManager[positionerName].position.values())[0]
            self._analogParameterDict['target_device'].append(positionerName)
            self._analogParameterDict['axis_length'].append(size)
            self._analogParameterDict['axis_step_size'].append(stepSize)
            self._analogParameterDict['axis_startpos'].append(start)

        self._digitalParameterDict['target_device'] = []
        self._digitalParameterDict['TTL_start'] = []
        self._digitalParameterDict['TTL_end'] = []
        for deviceName, deviceInfo in self.TTLDevices.items():
            if not self._widget.getTTLIncluded(deviceName):
                continue
            self._digitalParameterDict['target_device'].append(deviceName)
            self._digitalParameterDict['TTL_start'].append(self._widget.getTTLStarts(deviceName)[0])
            self._digitalParameterDict['TTL_end'].append(self._widget.getTTLEnds(deviceName)[0])

        self._digitalParameterDict['sequence_time'] = self._widget.getSeqTimePar()

    def updateSteps(self):
        self.getParameters()
        for index, positionerName in enumerate(self.positioners):
            if float(self._analogParameterDict['axis_step_size'][index]) != 0:
                steps = round(float(self._analogParameterDict['axis_length'][index]) /
                               float(self._analogParameterDict['axis_step_size'][index]))
                self._widget.setScanSteps(positionerName, steps)

    def plotSignalGraph(self):
        dwellTime = float(self._widget.seqTimePar.text())
        graphSamples = 10000
        areas = []
        signals = []
        colors = []
        for deviceName in self.TTLDevices.keys():
            isLaser = deviceName in self._setupInfo.lasers
            x = np.linspace(0, dwellTime, graphSamples)
            signal = np.zeros(graphSamples)
            try:
                start = float(self._widget.pxParameters['sta' + deviceName].text())
                end = float(self._widget.pxParameters['end' + deviceName].text())
                signal[x > start] = 1
                signal[x > end] = 0
            except ValueError:
                pass
            areas.append(x)
            signals.append(signal)
            colors.append(
                colorutils.wavelengthToHex(
                    self._setupInfo.lasers[deviceName].wavelength
                ) if isLaser else '#ffffff'
            )
        self._widget.plotSignalGraph(areas, signals, colors)

    def emitScanSignal(self, signal, *args):
        signal.emit(*args)

    def attrChanged(self, key, value):
        if self.settingAttr or len(key) != 2:
            return
        if key[0] == _attrCategoryStage:
            self._analogParameterDict[key[1]] = value
            self.setParameters()
        elif key[0] == _attrCategoryTTL:
            self._digitalParameterDict[key[1]] = value
            self.setParameters()

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
            positiveDirection = self._setupInfo.positioners[positionerName].isPositiveDirection
            positiveDirections.append(positiveDirection)
        self.setSharedAttr(_attrCategoryStage, 'positive_direction', positiveDirections)

    def updateScanTTLAttrs(self):
        self.getParameters()
        for key, value in self._digitalParameterDict.items():
            self.setSharedAttr(_attrCategoryTTL, key, value)

    def _getScanLaserDevices(self):
        """Return the names of the lasers included in this scan.

        These are the TTL-included devices that are also registered as
        lasers in the setup. The LaserController uses this list (via
        sigScanBuilt) to arm exactly these lasers for the scan.
        """
        self.getParameters()
        return [d for d in self._digitalParameterDict['target_device']
                if d in self._setupInfo.lasers]

    def runScan(self) -> None:
        """Runs a scan with the set scanning parameters."""
        self.runScanAdvanced(sigScanStartingEmitted=False)


_attrCategoryStage = 'ScanStage'
_attrCategoryTTL = 'ScanTTL'
