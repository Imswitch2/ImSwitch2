import json
import traceback
import configparser
from typing import Dict, Any
import numpy as np
from imswitch.imcommon.model import APIExport
from ast import literal_eval

from ..basecontrollers import SuperScanController, ComponentStateApplyMode
from imswitch.imcontrol.model.scan_parameters import pixels_for_length_step
from imswitch.imcommon.view.guitools import colorutils
from PyQt5.QtCore import QTimer
import copy
from imswitch.imcommon.model import APIExport
from imswitch.imcontrol.model import getWidgetStatePersistence


class ScanControllerMoNaLISA(SuperScanController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._widget.initControls(
            self.positioners.keys(),
            self.TTLDevices.keys(),
            self._master.scanManager.TTLTimeUnits
        )

        self.updatePixels()
        self.plotSignalGraph()
        self.updateScanStageAttrs()
        self.updateScanTTLAttrs()

        self.awaitingPipeline = False
        self._pipelineRunToken = None
        self.autoAxial = False
        self.pipeline_timeout_ms = 3000

        self._analogParameterDictXY = None

        # QTimer to handle timeouts
        self.pipelineTimeoutTimer = QTimer()
        self.pipelineTimeoutTimer.setSingleShot(True)
        self.pipelineTimeoutTimer.timeout.connect(self.onPipelineTimeout)

        # Connect ScanWidget signals
        self._widget.sigContLaserPulsesToggled.connect(self.setContLaserPulses)
        self._widget.sigSeqTimeParChanged.connect(self.plotSignalGraph)
        self._widget.sigSignalParChanged.connect(self.plotSignalGraph)

        # widget signal sent to commChannel
        self._widget.sigUpdateBeadRecCenter.connect(self._commChannel.beadRecWorkflow.update_bead_rec_center)
        self._widget.sigShowBeadRecCenterCross.connect(self._commChannel.beadRecWorkflow.show_bead_rec_center_cross)
        self._widget.sigAutoAxialToggled.connect(self._commChannel.beadRecWorkflow.set_auto_axial)

        self._commChannel.beadRecWorkflow.on_center_coord_pipeline_finished(self.centerCoordPipelineFinished)

        getWidgetStatePersistence().register('Scan', self)

    def getDimsScan(self):
        # TODO: Make sure this works as intended
        self.getParameters()

        lengths = self._analogParameterDict['axis_length']
        stepSizes = self._analogParameterDict['axis_step_size']

        # round(len/step) via the shared helper (0 = inactive axis), matching the
        # scan designers, the widget pixel display, and the recorded OME dims.
        x = pixels_for_length_step(lengths[0], stepSizes[0]) if stepSizes[0]!=0 else 0
        y = pixels_for_length_step(lengths[1], stepSizes[1]) if stepSizes[1]!=0 else 0
        z = pixels_for_length_step(lengths[2], stepSizes[2]) if stepSizes[2]!=0 else 0

        return x, y, z

    def getScanStepSizes(self):
        return self._analogParameterDict['axis_step_size']

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
                self._widget.setScanCenterPos(positionerName,
                                              self._analogParameterDict['axis_centerpos'][i])

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

    def runScanAdvanced(self, *, recalculateSignals=True, isNonFinalPartOfSequence=False,
                        sigScanStartingEmitted,axialFollowUp=False):
        """ Runs a scan with the set scanning parameters. """

        try:
            if self._beginScanRun(
                sigScanStartingEmitted=sigScanStartingEmitted
            ) is None:
                return
            self._widget.setScanButtonChecked(True)
            # Do not mutate axial buffers until this exact start has acquired
            # run ownership. A duplicate user/API start while another
            # iteration is active must be a side-effect-free refusal.
            if not axialFollowUp:
                self.checkAxialAutoScan()
                if self.autoAxial:
                    self.setupAxial()

            if recalculateSignals or self.signalDict is None or self.scanInfoDict is None:
                self.getParameters()
                try:
                    self.signalDict, self.scanInfoDict = self._master.scanManager.makeFullScan(
                        self._analogParameterDict, self._digitalParameterDict,
                        staticPositioner=self._widget.isContLaserMode()
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
                    self._logger.debug(f'set {positionerName} center to {position} before scan')
            # run scan
            self._armScanIteration(self.signalDict, self.scanInfoDict)
        except Exception:
            self._logger.error(traceback.format_exc())
            self.scanFailed()

    def resetPositioners(self):
        """ For when 'center' is not 0: put back positioner in position before the scan.
        Without this, positioner will be left at 'center' position at end of scan."""
        for index, positionerName in enumerate(self._analogParameterDict['target_device']):
            if positionerName in self._positionersScan:
                center = self._analogParameterDict['axis_centerpos'][index]
                if center!=0:
                    self._master.positionersManager[positionerName].resetToCurrent()

    def scanDone(self):
        self.isRunning = False
        try:
            self.resetPositioners()
            if self.autoAxial and len(self.axialListBuffer)!=0:
                self.nextAxial = self.axialListBuffer.pop(0)
                if self.centerCoord is None:
                    self.getCenterCoord()
                else:
                    self.runNextAxialScan()

            else:
                if self.autoAxial:
                    self.resetAfterAutoAxialFinished()

                if not self._widget.isContLaserMode() and not self._widget.repeatEnabled():
                    isFinalPart = not self.doingNonFinalPartOfSequence
                    if isFinalPart:
                        try:
                            self._widget.setScanButtonChecked(False)
                        except Exception:
                            self._logger.error(
                                'Failed to reset the scan widget after '
                                'completion',
                                exc_info=True,
                            )
                    self._publishScanDone(isFinalPart=isFinalPart)
                else:
                    # Defer the re-arm so the finished scan's NI-DAQ tasks and
                    # detector threads tear down first (see _armRepeatScan).
                    self._armRepeatScan()
        except Exception:
            self._logger.error(traceback.format_exc())
            self.scanFailed()

    def _shouldContinueRepeat(self) -> bool:
        if getattr(self, '_scanStopRequested', False):
            return False
        return self._widget.isContLaserMode() or self._widget.repeatEnabled()

    def getCenterCoord(self):
        if self.centerSearchMode == "Manual":
            x = int(self._widget.xCenterEdit.text())
            y = int(self._widget.yCenterEdit.text())
            self.centerCoord = self.convertToUm((y,x))
            self.runNextAxialScan()
        else:
            self.awaitingPipeline = True
            self._pipelineRunToken = self.__dict__.get('_scanRunToken')
            self._commChannel.beadRecWorkflow.query_center_coord(self.centerSearchMode)
            self.pipelineTimeoutTimer.start(self.pipeline_timeout_ms)# Start timeout

    def centerCoordPipelineFinished(self,coord):
        pipelineRunToken = self.__dict__.get('_pipelineRunToken')
        if (
            not self.awaitingPipeline
            or pipelineRunToken is None
            or self.__dict__.get('_scanRunToken') is not pipelineRunToken
            or self._scanCoordinator.runForOwner(self) is not pipelineRunToken
        ):
            return
        try:
            self.pipelineTimeoutTimer.stop()
            self.awaitingPipeline = False
            self._pipelineRunToken = None
            if coord is None:
                self.axialListBuffer = []
                self.scanDone()
            else:
                self._widget.yCenterEdit.setText(str(int(coord[0])))
                self._widget.xCenterEdit.setText(str(int(coord[1])))
                self.centerCoord = self.convertToUm(coord)
                self.runNextAxialScan()
        except Exception:
            # The active NI iteration has already finished while this run token
            # is intentionally retained for the axial follow-up. Any callback
            # failure must terminalize that idle reservation.
            self.awaitingPipeline = False
            self._pipelineRunToken = None
            try:
                self.pipelineTimeoutTimer.stop()
            except Exception:
                pass
            self._logger.error(traceback.format_exc())
            self.scanFailed()

    def onPipelineTimeout(self):
        pipelineRunToken = self.__dict__.get('_pipelineRunToken')
        if (
            self.awaitingPipeline
            and pipelineRunToken is not None
            and self.__dict__.get('_scanRunToken') is pipelineRunToken
            and self._scanCoordinator.runForOwner(self) is pipelineRunToken
        ):
            self._logger.warning("Pipeline analysis timed out! Proceeding without axial scan.")
            self.awaitingPipeline = False
            self._pipelineRunToken = None
            self.axialListBuffer = []
            self.scanDone()
        else:
            return

    def runNextAxialScan(self):
        try:
            isNonFinalPartOfSequence = (
                self.doingNonFinalPartOfSequence
            )
            self.updateScanParamForAxial()
            # autoAxial is one user-visible run made of several scan
            # iterations. sigScanStarting was emitted before the first
            # iteration and sigScanEnded is emitted only after the final one.
            self.runScanAdvanced(
                isNonFinalPartOfSequence=isNonFinalPartOfSequence,
                sigScanStartingEmitted=True,
                axialFollowUp=True,
            )
        except Exception:
            self._logger.error(traceback.format_exc())
            self.scanFailed()

    def abortScan(self):
        """Cancel delayed axial callbacks before releasing run ownership."""
        self.awaitingPipeline = False
        self._pipelineRunToken = None
        self.axialListBuffer = []
        try:
            self.pipelineTimeoutTimer.stop()
        except Exception:
            pass
        super().abortScan()

    def checkAxialAutoScan(self):
        try:
            if self._widget.AutoXZScanBox.isChecked() or self._widget.AutoYZScanBox.isChecked():
                x,y,z = self.getDimsScan()
                if (x>1 and y>1 and z < 2):
                    self.autoAxial = True
                else:
                    self._logger.info("Auto axial scan only available for a 2d XY scan")
                    self.autoAxial = False
        except Exception as e:
            self.autoAxial = False

    def setupAxial(self):
        self.axialListBuffer=[]
        if self._widget.AutoXZScanBox.isChecked():
            self.axialListBuffer.append("XZ")
        if self._widget.AutoYZScanBox.isChecked():
            self.axialListBuffer.append("YZ")
        self.nextAxial = "XY"
        self.centerSearchMode = self._widget.axialMenu.currentText()
        self.centerCoord = None
        self._commChannel.beadRecWorkflow.set_axial_list_buffer(self.axialListBuffer)

    def resetAfterAutoAxialFinished(self):
        if self._analogParameterDictXY is not None:
            self._analogParameterDict = copy.deepcopy(self._analogParameterDictXY)
            self._analogParameterDictXY = None
            self.setParameters()
        self.centerCoord = None

    def updateScanParamForAxial(self):
        if self.centerCoord is None: #should never happen though
            self._logger.error("Could not update scan parameter for axial because self.centercoord = None")
            return

        # first we save XY scan parameters
        if self._analogParameterDictXY is None:
            self._analogParameterDictXY = copy.deepcopy(self._analogParameterDict)
        else:
            self._analogParameterDict = copy.deepcopy(self._analogParameterDictXY)

        # keep only X or Y scan, put the other one at center position
        if self.nextAxial == "XZ":
            static = self._analogParameterDictXY['target_device'].index('Y')
            centerValue = -1*self.centerCoord[0] + self._analogParameterDictXY['axis_centerpos'][static]
        elif self.nextAxial == "YZ":
            static = self._analogParameterDictXY['target_device'].index('X')
            centerValue = -1*self.centerCoord[1] + self._analogParameterDictXY['axis_centerpos'][static]

        for key, value_list in self._analogParameterDict.items():
            if key not in ['target_device','axis_centerpos'] and isinstance(value_list, list):
                self._analogParameterDict[key][static] = 0.0
        self._analogParameterDict['axis_centerpos'][static] = centerValue

        # transfer axial parameters into the Z axis
        zIdx = self._analogParameterDict['target_device'].index('Z')
        size = self._widget.getScanSize('Axial')
        stepSize = self._widget.getScanStepSize('Axial')
        center = self._widget.getScanCenterPos('Axial') + size/2 # by default at middle position
        start = list(self._master.positionersManager['Z'].position.values())
        self._analogParameterDict['axis_length'][zIdx] = size
        self._analogParameterDict['axis_step_size'][zIdx] = stepSize
        self._analogParameterDict['axis_centerpos'][zIdx] = center
        self._analogParameterDict['axis_startpos'][zIdx] = start

        self.setParameters()

    def convertToUm(self,coord):
        yIndex = self._analogParameterDict['target_device'].index('Y')
        xIndex = self._analogParameterDict['target_device'].index('X')
        yCoord = coord[0]*self._analogParameterDict['axis_step_size'][yIndex]
        xCoord = coord[1]*self._analogParameterDict['axis_step_size'][xIndex]
        return (yCoord,xCoord)

    def getNextAxial(self):
        if self.autoAxial:
            return self.nextAxial
        else:
            return None

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
        self._digitalParameterDict['TTL_start'] = []
        self._digitalParameterDict['TTL_end'] = []
        for deviceName, _ in self.TTLDevices.items():
            if not self._widget.getTTLIncluded(deviceName):
                continue

            self._digitalParameterDict['target_device'].append(deviceName)
            self._digitalParameterDict['TTL_start'].append(self._widget.getTTLStarts(deviceName))
            self._digitalParameterDict['TTL_end'].append(self._widget.getTTLEnds(deviceName))

        self._digitalParameterDict['sequence_time'] = self._widget.getSeqTimePar()
        self._analogParameterDict['sequence_time'] = self._widget.getSeqTimePar()

    def setContLaserPulses(self, isContLaserPulses):
        for i in range(len(self.positioners)):
            positionerName = self._widget.scanPar['scanDim' + str(i)].currentText()
            self._widget.setScanDimEnabled(i, not isContLaserPulses)
            self._widget.setScanSizeEnabled(positionerName, not isContLaserPulses)
            self._widget.setScanStepSizeEnabled(positionerName, not isContLaserPulses)
            self._widget.setScanCenterPosEnabled(positionerName, not isContLaserPulses)

    def updatePixels(self):
        self.getParameters()
        for index, positionerName in enumerate(self.positioners):
            if float(self._analogParameterDict['axis_step_size'][index]) != 0:
                pixels = round(float(self._analogParameterDict['axis_length'][index]) /
                               float(self._analogParameterDict['axis_step_size'][index]))
                self._widget.setScanPixels(positionerName, pixels)

        # update the optional axial Z
        try:
            stepSize = float(self._widget.scanPar['stepSizeAxial'].text())
            if stepSize !=0:
                length = float(self._widget.scanPar['sizeAxial'].text())
                pixels = round(float(length)/float(stepSize))
                self._widget.scanPar['pixelsAxial'].setText(str(pixels))
        except Exception as e:
            self._logger.debug(f"Failed to update axial pixels: {e}")
            pass



    def plotSignalGraph(self):
        if self.settingParameters:
            return

        self.getParameters()
        TTLCycleSignalsDict = self._master.scanManager.getTTLCycleSignalsDict(
            self._digitalParameterDict
        )

        sampleRate = self._master.scanManager.sampleRate

        areas = []
        signals = []
        colors = []
        for deviceName, signal in TTLCycleSignalsDict.items():
            isLaser = deviceName in self._setupInfo.lasers
            areas.append(
                np.linspace(
                    0, self._digitalParameterDict['sequence_time'] * sampleRate, len(signal)
                )
            )
            signals.append(signal.astype(np.uint8))
            colors.append(
                colorutils.wavelengthToHex(
                    self._setupInfo.lasers[deviceName].wavelength
                ) if isLaser else '#ffffff'
            )
        self._widget.plotSignalGraph(areas, signals, colors, sampleRate)

    def emitScanSignal(self, signal, *args):
        if not self._widget.isContLaserMode():  # Cont. laser pulses mode is not a real scan
            signal.emit(*args)

    # ------------------------------------------------------------------
    # Widget State Persistence Interface
    # ------------------------------------------------------------------



    def getStateSchemaVersion(self) -> int:
        return 1


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
