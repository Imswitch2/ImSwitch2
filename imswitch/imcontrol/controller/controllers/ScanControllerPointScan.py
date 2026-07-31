import json
import traceback
import configparser

from ast import literal_eval
from typing import Dict, Any

from ..basecontrollers import SuperScanController, ComponentStateApplyMode
from imswitch.imcommon.model import APIExport
from imswitch.imcontrol.model import getWidgetStatePersistence
from imswitch.imcontrol.model.scan_parameters import pixels_for_length_step

class ScanControllerPointScan(SuperScanController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._widget.initControls(
            self.positioners.keys(),
            self.TTLDevices.keys()
        )

        self.updatePixels()
        self.updateScanStageAttrs()
        self.updateScanTTLAttrs()

        getWidgetStatePersistence().register('Scan', self)

    def setParameters(self):
        self.settingParameters = True
        try:
            for i in range(len(self._analogParameterDict['target_device'])):
                positionerName = self._analogParameterDict['target_device'][i]
                self._widget.setScanSize(positionerName,
                                         self._analogParameterDict['axis_length'][i])
                self._widget.setScanStepSize(positionerName,
                                             self._analogParameterDict['axis_step_size'][i])
                self._widget.setScanCenterPos(positionerName,
                                              self._analogParameterDict['axis_centerpos'][i])
            for i in range(len(self._analogParameterDict['scan_dim_target_device'])):
                scanDimName = self._analogParameterDict['scan_dim_target_device'][i]
                self._widget.setScanDim(i, scanDimName)

            setTTLDevices = []
            for i in range(len(self._digitalParameterDict['target_device'])):
                deviceName = self._digitalParameterDict['target_device'][i]
                self._widget.setTTLSequences(deviceName, self._digitalParameterDict['TTL_sequence'][i])
                self._widget.setTTLSequenceAxis(deviceName, self._digitalParameterDict['TTL_sequence_axis'][i])
                setTTLDevices.append(deviceName)

            for deviceName in self.TTLDevices:
                if deviceName not in setTTLDevices:
                    self._widget.unsetTTL(deviceName)

            self._widget.setSeqTimePar(self._digitalParameterDict['sequence_time'])
            self._widget.setPhaseDelayPar(self._analogParameterDict['phase_delay'])
        finally:
            self.settingParameters = False
    
    def runScanAdvanced(self, *, recalculateSignals=True, isNonFinalPartOfSequence=False,
                        sigScanStartingEmitted):
        """ Runs a scan with the set scanning parameters. """
        try:
            if self._beginScanRun(
                sigScanStartingEmitted=sigScanStartingEmitted
            ) is None:
                return
            self._widget.setScanButtonChecked(True)

            if recalculateSignals or self.signalDict is None or self.scanInfoDict is None:
                self.getParameters()
                try:
                    self.signalDict, self.scanInfoDict = self._master.scanManager.makeFullScan(
                        self._analogParameterDict, self._digitalParameterDict
                    )
                except TypeError:
                    self._logger.error(traceback.format_exc())
                    self.scanFailed()
                    return

            self.doingNonFinalPartOfSequence = isNonFinalPartOfSequence

            # set positions of scanners not in scan from centerpos
            for index, positionerName in enumerate(self._analogParameterDict['target_device']):
                if positionerName not in self._positionersScan:
                    position = self._analogParameterDict['axis_centerpos'][index]
                    self._master.positionersManager[positionerName].setPosition(position, 0)
                    #self._logger.debug(f'Set {positionerName} center to {position} before scan')
            # run scan
            self._armScanIteration(self.signalDict, self.scanInfoDict)
        except Exception:
            self._logger.error(traceback.format_exc())
            self.scanFailed()

    def scanDone(self):
        self.isRunning = False
        try:
            if not self._widget.repeatEnabled():
                isFinalPart = not self.doingNonFinalPartOfSequence
                try:
                    self._resetReturnToCenterPositionersAfterScan()
                except Exception:
                    self._logger.warning(
                        "Failed to reset positioners after scan:\n%s",
                        traceback.format_exc(),
                    )
                if isFinalPart:
                    try:
                        self._widget.setScanButtonChecked(False)
                    except Exception:
                        self._logger.error(
                            'Failed to reset the scan widget after completion',
                            exc_info=True,
                        )
                self._publishScanDone(isFinalPart=isFinalPart)
            else:
                # Defer the re-arm so the finished scan's NI-DAQ tasks and
                # detector threads tear down before the next frame starts.
                self._armRepeatScan()
        except Exception:
            self._logger.error(traceback.format_exc())
            self.scanFailed()

    def getDimsScan(self):
        """Return (x, y, z) pixel counts for the first three scan axes."""
        self.getParameters()
        lengths = self._analogParameterDict.get('axis_length', [])
        stepSizes = self._analogParameterDict.get('axis_step_size', [])
        dims = []
        for i in range(min(3, len(lengths))):
            step = stepSizes[i] if i < len(stepSizes) else 0
            dims.append(
                pixels_for_length_step(lengths[i], step) if step != 0 else 0
            )
        while len(dims) < 3:
            dims.append(0)
        return tuple(dims[:3])

    def getScanStepSizes(self):
        """Return (x, y, z) step sizes for recording metadata."""
        stepSizes = self._analogParameterDict.get('axis_step_size', [])
        result = list(stepSizes[:3])
        while len(result) < 3:
            result.append(0.0)
        return result

    def getParameters(self):
        if self.settingParameters:
            return
        self._analogParameterDict['target_device'] = []
        self._analogParameterDict['axis_length'] = []
        self._analogParameterDict['axis_step_size'] = []
        self._analogParameterDict['axis_centerpos'] = []
        self._analogParameterDict['axis_startpos'] = []
        self._positionersScan = []
        for i in range(len(self.positioners)):
            self._positionersScan.append(self._widget.getScanDim(i))
        self._analogParameterDict['scan_dim_target_device'] = self._positionersScan
        for positionerName in self._positionersScan:
            if positionerName != 'None':
                size = self._widget.getScanSize(positionerName)
                stepSize = self._widget.getScanStepSize(positionerName)
                center = self._widget.getScanCenterPos(positionerName)
                start = list(self._master.positionersManager[positionerName].position.values())
                self._analogParameterDict['target_device'].append(positionerName)
                self._analogParameterDict['axis_length'].append(size)
                self._analogParameterDict['axis_step_size'].append(stepSize)
                self._analogParameterDict['axis_centerpos'].append(center)
                self._analogParameterDict['axis_startpos'].append(start)
        for positionerName in self.positioners:
            if positionerName not in self._positionersScan:
                size = 1.0
                stepSize = 1.0
                center = self._widget.getScanCenterPos(positionerName)
                start = [0]
                self._analogParameterDict['target_device'].append(positionerName)
                self._analogParameterDict['axis_length'].append(size)
                self._analogParameterDict['axis_step_size'].append(stepSize)
                self._analogParameterDict['axis_centerpos'].append(center)
                self._analogParameterDict['axis_startpos'].append(start)
        self._digitalParameterDict['target_device'] = []
        self._digitalParameterDict['TTL_sequence'] = []
        self._digitalParameterDict['TTL_sequence_axis'] = []
        for deviceName, _ in self.TTLDevices.items():
            if not self._widget.getTTLIncluded(deviceName):
                continue

            self._digitalParameterDict['target_device'].append(deviceName)
            self._digitalParameterDict['TTL_sequence'].append(self._widget.getTTLSequence(deviceName))
            self._digitalParameterDict['TTL_sequence_axis'].append(self._widget.getTTLSequenceAxis(deviceName))

        self._digitalParameterDict['sequence_time'] = self._widget.getSeqTimePar()
        self._analogParameterDict['sequence_time'] = self._widget.getSeqTimePar()
        self._analogParameterDict['phase_delay'] = self._widget.getPhaseDelayPar()
        self._analogParameterDict['d3step_delay'] = self._widget.getd3StepDelayPar()
        #self._analogParameterDict['extra_laser_on'] = self._widget.getExtraLaserOnPar()

    def updatePixels(self):
        self.getParameters()
        for index, positionerName in enumerate(self._analogParameterDict['target_device']):
            if float(self._analogParameterDict['axis_step_size'][index]) != 0:
                pixels = round(float(self._analogParameterDict['axis_length'][index]) /
                               float(self._analogParameterDict['axis_step_size'][index]))
                self._widget.setScanPixels(positionerName, pixels)

    def emitScanSignal(self, signal, *args):
        signal.emit(*args)

    @APIExport(runOnUIThread=True)
    def changed3StepDelayPar(self, d3StepDelay): #Simone: Simone added this to allow imscripting
        self._widget.setd3StepDelayPar(d3StepDelay)

    @APIExport(runOnUIThread=True)
    def changeScanCenterPos(self, positionerName, positionerScanCenterPos): #Simone added this to allow imscripting
        self._widget.setScanCenterPos(positionerName, positionerScanCenterPos)

    @APIExport(runOnUIThread=True)
    def changeScanSize(self, positioner: str, size: float): #Simone added this to allow imscripting
        self._widget.setScanSize(positioner, size)

    # ------------------------------------------------------------------
    # Widget State Persistence Interface
    # ------------------------------------------------------------------



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
