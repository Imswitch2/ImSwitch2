import os
import json
import configparser
from ast import literal_eval
from ..basecontrollers import ImConWidgetController, StatefulComponentMixin, ComponentStateApplyMode
from ._triggerscope_scan_geometry import check_dac_range
import numpy as np
import traceback
from imswitch.imcommon.model import APIExport, dirtools, initLogger
from imswitch.imcontrol.model import getWidgetStatePersistence
from imswitch.imcontrol.view import guitools
from imswitch.imcommon.view.guitools import colorutils
from ._beadrec_scan_source import BeadRecScanSourceMixin
from ._triggerscope_scan_lifecycle import TriggerScopeScanLifecycleMixin
from ._acquisition_layout_source import (
    build_triggerscope_raster_layouts,
    scan_driven_detector_names,
)


def trimRasterLengthForFirmwareBoundary(length, stepSize):
    """Trim a raster axis length by half a step before sending it to firmware.

    The TriggerScope firmware's RASTER_SCAN loop is INCLUSIVE
    (``while (abs(pos) <= abs(lenV))``), so when ``length`` is an exact
    multiple of ``stepSize`` it visits ``length/stepSize + 1`` positions (both
    endpoints) -- one MORE pixel than the ``round(length/stepSize)`` pixel
    count the rest of the system (BeadRec reconstruction dims, the GUI step
    count) expects. This is what made e.g. a 4 um scan at exactly 200 nm/pixel
    (20.0 pixels) come back with 21 frames, while nudging to 201 nm/pixel
    (not an exact divisor) "fixed" it by accident -- the loop then overshoots
    the threshold one increment earlier and stops at the intended count.

    Shrinking the length sent to firmware by half a step keeps the boundary
    check comfortably between the intended last position
    ``(N-1)*stepSize`` and the next one ``N*stepSize``, so the firmware visits
    exactly ``N = round(length/stepSize)`` positions regardless of which side
    of an exact ratio floating-point rounding happens to fall on. No firmware
    change needed -- this only adjusts what gets sent.
    """
    if stepSize == 0:
        return length
    return length - 0.5 * stepSize


class TriggerScopeRasterController(
    StatefulComponentMixin,
    BeadRecScanSourceMixin,
    TriggerScopeScanLifecycleMixin,
    ImConWidgetController,
):
    """Linked to TriggerScopeRasterWidget."""

    componentName = 'Scan'
    stateSchemaVersion = 1
    legacyStateNames = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._logger = initLogger(self)

        self.settingAttr = False
        self.settingParameters = False

        self._analogParameterDict = {}
        self._digitalParameterDict = {}
        self.signalDict = None
        self.scanInfoDict = None
        self.isRunning = False
        self.doingNonFinalPartOfSequence = False
        # Set by abortScan while the firmware is mid-scan; consumed by
        # scanDone to suppress the repeat re-arm. See abortScan.
        self._scanStopRequested = False

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

        self._initTriggerScopeScanLifecycle()

        self._commChannel.sigRunScan.connect(self.runScanExternal)
        self._commChannel.sigAbortScan.connect(self.abortScan)
        self._commChannel.sharedAttrs.sigAttributeSet.connect(self.attrChanged)

        self._widget.sigSaveScanClicked.connect(self.saveScan)
        self._widget.sigLoadScanClicked.connect(self.loadScan)
        self._widget.sigRunScanClicked.connect(self.runScan)
        self._widget.sigAbortScanClicked.connect(self.abortScan)
        self._widget.sigForceStopScanClicked.connect(self.forceStopScan)
        self._widget.sigSeqTimeParChanged.connect(self.plotSignalGraph)
        self._widget.sigSeqTimeParChanged.connect(self.updateScanTTLAttrs)
        self._widget.sigStageParChanged.connect(self.updateSteps)
        self._widget.sigStageParChanged.connect(self.updateScanStageAttrs)
        self._widget.sigSignalParChanged.connect(self.plotSignalGraph)
        self._widget.sigSignalParChanged.connect(self.updateScanTTLAttrs)

        getWidgetStatePersistence().register('Scan', self)

    def saveScan(self):
        fileName = guitools.askForFilePath(self._widget, 'Save scan', self.scanDir, isSaving=True)
        if not fileName:
            return
        self.saveScanParamsToFile(fileName)

    @APIExport(runOnUIThread=True)
    def saveScanParamsToFile(self, filePath: str) -> None:
        """Saves the set scanning parameters to the specified file."""
        if not filePath.endswith('.json'):
            filePath += '.json'
        state = self.getComponentState()
        try:
            with open(filePath, 'w') as f:
                json.dump(state, f, indent=2)
            self._logger.info(f'Scan parameters saved to {filePath}')
        except Exception:
            self._logger.error(f'Failed to save scan parameters:\n{traceback.format_exc()}')

    def loadScan(self):
        fileName = guitools.askForFilePath(self._widget, 'Load scan', self.scanDir)
        if not fileName:
            return
        self.loadScanParamsFromFile(fileName)

    @APIExport(runOnUIThread=True)
    def loadScanParamsFromFile(self, filePath: str) -> None:
        """Loads scanning parameters from the specified file."""
        payload = self._read_scan_file(filePath)
        if payload is None:
            return
        warnings = self.applyComponentState(payload, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY)
        if warnings:
            for warning in warnings:
                self._logger.warning(warning)

    def _read_scan_file(self, filePath: str):
        """Read a scan file, returning a component state dict.
        
        Tries JSON first; falls back to the legacy configparser INI format.
        Returns None on unrecoverable error.
        """
        try:
            with open(filePath, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
        except Exception:
            self._logger.error(f'Could not open scan file {filePath!r}:\n{traceback.format_exc()}')
            return None

        try:
            config = configparser.ConfigParser()
            config.optionxform = str
            config.read(filePath)
            analogParameterDict = {}
            digitalParameterDict = {}
            if 'analogParameterDict' in config._sections:
                for key, value in config._sections['analogParameterDict'].items():
                    analogParameterDict[key] = literal_eval(value)
            if 'digitalParameterDict' in config._sections:
                for key, value in config._sections['digitalParameterDict'].items():
                    digitalParameterDict[key] = literal_eval(value)
            return {
                'controller': 'TriggerScopeRasterController',
                'scanWidgetType': 'TriggerScopeRaster',
                'analogParameterDict': analogParameterDict,
                'digitalParameterDict': digitalParameterDict,
            }
        except Exception:
            self._logger.error(f'Could not parse legacy scan file {filePath!r}:\n{traceback.format_exc()}')
            return None

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
            length = self._analogParameterDict['axis_length'][index]
            stepSize = self._analogParameterDict['axis_step_size'][index]
            # See trimRasterLengthForFirmwareBoundary: avoids an extra pixel
            # from the firmware's inclusive end-of-axis check.
            trimmedLength = trimRasterLengthForFirmwareBoundary(length, stepSize)
            lengthsVolt.append(trimmedLength / convFactor)
            stepSizesVolt.append(stepSize / convFactor)
            startPosVolt.append(self._analogParameterDict['axis_startpos'][index] / convFactor)

        for target, startVolt, lengthVolt in zip(AOtargets, startPosVolt, lengthsVolt):
            check_dac_range(self.positioners, target, (startVolt, startVolt + lengthVolt),
                            what='Raster scan')
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
            self._logRasterTTLDiagnostics()
            triggerscopeParameters = self.getTriggerscopeParameters()
            self._startTriggerScopeScan(
                parameters=triggerscopeParameters,
                scanType='rasterScan',
                laserDevices=self._getScanLaserDevices(),
                sigScanStartingEmitted=sigScanStartingEmitted,
                isNonFinalPartOfSequence=isNonFinalPartOfSequence,
            )
        except Exception:
            self._logger.error(traceback.format_exc())
            self.scanFailed()

    def abortScan(self):
        # The firmware runs the scan autonomously and exposes no abort
        # command, so the iteration already under way always finishes. What
        # an abort can and must do is stop the repeat loop from arming the
        # next one: without this, an abort raised while Repeat is ticked
        # (the RecordingController raises one whenever a scan-driven
        # recording stops) was swallowed entirely and the scanner kept
        # cycling with the recording long gone.
        self._requestTriggerScopeStop()

    def forceStopScan(self):
        """Force local teardown after an ordinary raster stop is pending.

        TriggerScope has no firmware abort command. This therefore ends the
        ImSwitch lifecycle and disarms scan-controlled lasers, but it cannot
        guarantee that physical scanning has stopped. Requiring the ordinary
        stop first makes this an explicit two-stage operator action.
        """
        if not self.isRunning or not self._scanStopRequested:
            self._logger.warning(
                'Ignoring raster force-stop because no running scan has a '
                'pending stop request'
            )
            return
        self._logger.error(
            'Forcing local raster teardown and laser disarm before '
            'TriggerScope reported "Scan done"; firmware motion may continue'
        )
        self.scanFailed()

    def scanDone(self):
        self._onTriggerScopeScanDone()

    def scanFailed(self):
        self._logger.error('Scan failed')
        self._failTriggerScopeScan()

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

    def _logRasterTTLDiagnostics(self):
        """Log what the firmware actually does with the TTL settings, and
        warn when a triggered camera cannot keep up with the dwell time.

        The deployed TriggerSwitch 0.1 firmware contains a "temporary fix"
        in runPixelCycle(): every pixel it pulses TTL lines 0-3 TOGETHER in
        a single window defined by the earliest TTL row's start/end times
        (sent as p1StartUs/p1EndUs). The per-device line selection (p1Line)
        and all other TTL rows are ignored, so which device sits on which of
        TTL0-3 does not affect the pulse pattern — emission control of the
        lasers comes from arming (sigScanDevicesResolved) alone.

        A camera in external frame-trigger mode silently drops triggers that
        arrive while it is still exposing/reading out — the typical symptom
        is roughly (but never exactly) half the expected frames.
        """
        included = self._digitalParameterDict.get('target_device', [])
        starts = self._digitalParameterDict.get('TTL_start', [])
        ends = self._digitalParameterDict.get('TTL_end', [])
        seqTime = self._digitalParameterDict.get('sequence_time')
        if not included or len(starts) != len(included) or not seqTime:
            return
        p1Index = starts.index(min(starts))
        p1End = ends[p1Index] if p1Index < len(ends) else float('nan')
        xDim, yDim = self.getBeadRecScanDims()
        self._logger.info(
            f'Raster scan TTL: firmware pulses TTL lines 0-3 together, '
            f'window from earliest row "{included[p1Index]}" '
            f'(start {starts[p1Index] * 1e3:.3f} ms, end {p1End * 1e3:.3f} ms, '
            f'dwell {seqTime * 1e3:.3f} ms). Individual TTL rows / line '
            f'selection are NOT honored by this firmware revision. '
            f'Expected camera frames: {xDim} x {yDim} = {xDim * yDim}.'
        )
        for detectorName in (d for d in included if d in self._setupInfo.detectors):
            try:
                detector = self._master.detectorsManager[detectorName]
                exposure = float(detector.parameters['Real exposure time'].value)
                readout = float(detector.parameters['Readout time'].value)
            except Exception:
                continue
            frameTime = exposure + readout
            if frameTime > seqTime:
                self._logger.warning(
                    f'Camera "{detectorName}" cannot keep up with the scan: '
                    f'exposure + readout = {frameTime * 1e3:.2f} ms exceeds '
                    f'the dwell time {seqTime * 1e3:.2f} ms. Triggers '
                    f'arriving during exposure/readout are dropped '
                    f'(typically ~half the frames). Increase the sequence '
                    f'time above {frameTime * 1e3:.2f} ms or reduce the '
                    f'exposure/ROI.'
                )

    def _getScanLaserDevices(self):
        """Return the names of the lasers included in this scan.

        These are the TTL-included devices that are also registered as
        lasers in the setup. The LaserController uses this list (via
        sigScanDevicesResolved) to arm exactly these lasers for the scan.
        """
        self.getParameters()
        return [d for d in self._digitalParameterDict['target_device']
                if d in self._setupInfo.lasers]

    def getBeadRecScanDims(self) -> tuple[int, int]:
        """Return scan dimensions as (X_pixels, Y_pixels) for BeadRec."""
        self.getParameters()
        axis_length = self._analogParameterDict.get('axis_length', [0, 0])
        axis_step_size = self._analogParameterDict.get('axis_step_size', [1, 1])
        
        x_dim = 0 if axis_step_size[0] == 0 else round(axis_length[0] / axis_step_size[0])
        y_dim = 0 if (len(axis_step_size) < 2 or axis_step_size[1] == 0) else round(axis_length[1] / axis_step_size[1])
        return (x_dim, y_dim)

    def getBeadRecStepSizes(self) -> tuple[float, float]:
        """Return scan step sizes as (X_step, Y_step) for BeadRec."""
        self.getParameters()
        axis_step_size = self._analogParameterDict.get('axis_step_size', [0.0, 0.0])
        x_step = axis_step_size[0] if len(axis_step_size) > 0 else 0.0
        y_step = axis_step_size[1] if len(axis_step_size) > 1 else 0.0
        return (x_step, y_step)

    def getNumScanPositions(self) -> int:
        """Return the total number of scan positions (pixels) of the raster
        scan. Consumed by the RecordingController in scan-once mode as the
        number of camera frames to record."""
        x_dim, y_dim = self.getBeadRecScanDims()
        return max(int(x_dim), 1) * max(int(y_dim), 1)

    def getNumCamTTL(self) -> dict:
        """Return camera TTL pulses per scan position, per detector.

        The raster firmware fires one p1 pulse per pixel, so every
        TTL-included detector receives at most one trigger per position.
        """
        self.getParameters()
        included = self._digitalParameterDict.get('target_device', [])
        return {d: 1 for d in included if d in self._setupInfo.detectors}

    def getAcquisitionLayouts(self, detectorNames):
        """Return the X-fast/Y-slow raster layout for each detector."""
        self.getParameters()
        directions = {}
        for kind, device in zip(
            ("scan_x", "scan_y"),
            self._analogParameterDict.get("target_device", ()),
        ):
            try:
                positive = bool(
                    self._setupInfo.positioners[device].isPositiveDirection
                )
            except Exception:
                continue
            directions[kind] = 1 if positive else -1
        return build_triggerscope_raster_layouts(
            detectorNames,
            dimensions=self.getBeadRecScanDims(),
            step_sizes=self.getBeadRecStepSizes(),
            pulse_counts=self.getNumCamTTL(),
            scan_source=type(self).__name__,
            scan_driven_detectors=scan_driven_detector_names(
                self, detectorNames
            ),
            directions=directions,
        )

    def runScan(self) -> None:
        """Runs a scan with the set scanning parameters."""
        self.runScanAdvanced(sigScanStartingEmitted=False)

    def getComponentState(self) -> dict:
        """Snapshot the current TriggerScope raster scan parameters."""
        self.getParameters()

        scanInfo = getattr(self._setupInfo, 'scan', None)

        return {
            'controller': type(self).__name__,
            'scanWidgetType': getattr(scanInfo, 'scanWidgetType', None),
            'analogParameterDict': dict(self._analogParameterDict),
            'digitalParameterDict': dict(self._digitalParameterDict),
        }

    def applyComponentState(self, state: dict, *, applyMode: ComponentStateApplyMode) -> list[str]:
        """Restore TriggerScope raster scan parameters from a snapshot.
        
        CRITICAL SAFETY INVARIANT: This method MUST NEVER start a scan in either mode.
        It only restores scan parameters (analogParameterDict, digitalParameterDict).
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

        analogParameterDict = state.get('analogParameterDict', {})
        digitalParameterDict = state.get('digitalParameterDict', {})

        if not isinstance(analogParameterDict, dict):
            return warnings + ['Saved analog scan parameters are not a dictionary.']
        if not isinstance(digitalParameterDict, dict):
            return warnings + ['Saved digital scan parameters are not a dictionary.']

        targetDevices = analogParameterDict.get('target_device', [])
        if not isinstance(targetDevices, list):
            targetDevices = [targetDevices] if targetDevices else []
        
        missingPositioners = [d for d in targetDevices if d not in self.positioners]
        if missingPositioners:
            warnings.append(
                f'Missing scan positioner(s): {", ".join(missingPositioners)}. '
                'Scan state was not applied.'
            )
            return warnings

        ttlDevices = digitalParameterDict.get('target_device', [])
        if not isinstance(ttlDevices, list):
            ttlDevices = [ttlDevices] if ttlDevices else []
        
        missingTTLDevices = [d for d in ttlDevices if d not in self.TTLDevices]
        if missingTTLDevices:
            warnings.append(
                f'Missing TTL device(s): {", ".join(missingTTLDevices)}. '
                'Scan state was not applied.'
            )
            return warnings

        self._analogParameterDict = dict(analogParameterDict)
        self._digitalParameterDict = dict(digitalParameterDict)

        try:
            self.setParameters()
            self.updateSteps()
            self.plotSignalGraph()
            self.updateScanStageAttrs()
            self.updateScanTTLAttrs()
        except Exception as e:
            self._logger.error('Failed to apply TriggerScope raster component state')
            self._logger.error(traceback.format_exc())
            warnings.append(f'Failed to apply scan state: {e}')

        return warnings

    def describeComponentState(self, state: dict) -> list[str]:
        """Generate a human-readable summary of a saved TriggerScope raster scan state."""
        if not isinstance(state, dict) or not state:
            return ["  no scan state"]

        analog = state.get("analogParameterDict") or {}
        digital = state.get("digitalParameterDict") or {}
        summaries = []

        if state.get("controller"):
            summaries.append(f"  controller: {state.get('controller')}")
        if state.get("scanWidgetType"):
            summaries.append(f"  widget type: {state.get('scanWidgetType')}")

        targetDevices = analog.get('target_device', [])
        if targetDevices:
            summaries.append(f"  scan axes: {targetDevices}")

        axisLength = analog.get('axis_length', [])
        axisStepSize = analog.get('axis_step_size', [])
        if axisLength and axisStepSize:
            for i, device in enumerate(targetDevices[:len(axisLength)]):
                length = axisLength[i] if i < len(axisLength) else 0
                step = axisStepSize[i] if i < len(axisStepSize) else 1
                steps = 0 if step == 0 else round(length / step)
                summaries.append(f"    {device}: length={length:.3f}, step={step:.3f}, steps={steps}")

        ttlDevices = digital.get('target_device', [])
        if ttlDevices:
            summaries.append(f"  TTL devices: {ttlDevices}")

        seqTime = digital.get('sequence_time')
        if seqTime is not None:
            summaries.append(f"  sequence time: {seqTime * 1e3:.3f} ms")

        ttlStarts = digital.get('TTL_start', [])
        ttlEnds = digital.get('TTL_end', [])
        if ttlStarts and ttlEnds:
            summaries.append("  TTL timing:")
            for i, device in enumerate(ttlDevices[:len(ttlStarts)]):
                start = ttlStarts[i] if i < len(ttlStarts) else 0
                end = ttlEnds[i] if i < len(ttlEnds) else 0
                summaries.append(f"    {device}: start={start * 1e3:.3f} ms, end={end * 1e3:.3f} ms")

        return summaries or ["  no scan state"]

    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None,
    ) -> list[dict]:
        """Identify potential hazards in a saved TriggerScope raster scan state.
        
        Scan parameters carry no laser-power-like hazards; laser hazards belong
        to the Laser component. Returns an empty list.
        """
        return []


_attrCategoryStage = 'ScanStage'
_attrCategoryTTL = 'ScanTTL'
