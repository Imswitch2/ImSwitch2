from typing import Mapping

import numpy as np
from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.imcommon.model import pythontools, APIExport, SharedAttributes
from imswitch.imcommon.model import initLogger
from .WorkflowServices import BeadRecWorkflowService, ScanWorkflowService


class CommunicationChannel(SignalInterface):
    """
    Signal bus and narrow API helper for imcontrol controllers.

    Keep this class as a compatibility surface. New cross-controller workflows
    should prefer scoped service objects instead of adding more unrelated
    signals here.
    """

    DEPRECATED_SIGNALS = {
        'sigGridToggled': 'No internal producer or consumer found in repository scan.',
        'sigCrosshairToggled': 'No internal producer or consumer found in repository scan.',
        'sigScanFrameFinished': 'No internal producer or consumer found in repository scan.',
        'sigClockWidefield': 'No internal producer or consumer found in repository scan.',
    }

    EVENT_GROUP_NAMES = (
        'acquisitionEvents',
        'viewEvents',
        'recordingEvents',
        'scanEvents',
        'snapshotEvents',
        'slmEvents',
        'focusEvents',
        'rotationEvents',
        'eventTriggeredEvents',
        'beadRecEvents',
        'useqEvents',
        'scriptEvents',
    )

    # Acquisition and image stream events. Producers are mainly the detector
    # managers, connected in MasterController.
    sigUpdateImage = Signal(
        str, np.ndarray, bool, list, bool
    )  # (detectorName, image, init, scale, isCurrentDetector)
    sigAcquisitionStarted = Signal()
    sigAcquisitionStopped = Signal()
    sigAdjustFrame = Signal(object)  # (shape)
    sigDetectorSwitched = Signal(str, str)  # (newDetectorName, oldDetectorName)
    sigNewFrame = Signal()

    # View-layer overlay and viewer item events.
    # Deprecated compatibility signals: sigGridToggled, sigCrosshairToggled.
    sigGridToggled = Signal(bool)  # (enabled)
    sigCrosshairToggled = Signal(bool)  # (enabled)
    sigAddItemToVb = Signal(object)  # (item)
    sigRemoveItemFromVb = Signal(object)  # (item)
    sigSetVisibleLayers = Signal(object)  # (detectorNameTuple) — Snouty setup switching
    sigSetConfig = Signal(str)  # (configName) — Snouty setup switching

    # Recording events.
    sigRecordingStarted = Signal()
    sigRecordingEnded = Signal()
    sigUpdateRecFrameNum = Signal(int)  # (frameNumber)
    sigUpdateRecTime = Signal(int)  # (recTime)
    sigMemorySnapAvailable = Signal(
        str, np.ndarray, object, bool
    )  # (name, image, filePath, savedToDisk)
    sigStartRecording = Signal()
    sigStopRecording = Signal()

    # Scan orchestration events. These are hardware-adjacent and must keep
    # their current semantics unless reviewed with hardware access.
    sigRunScan = Signal(bool, bool)  # (recalculateSignals, isNonFinalPartOfSequence)
    sigAbortScan = Signal()
    sigScanStarting = Signal()
    sigScanBuilt = Signal(object)  # (deviceList)
    sigScanStarted = Signal()
    sigScanDone = Signal()
    sigScanEnded = Signal()
    sigToggleBlockScanWidget = Signal(bool)
    sigRequestScanParameters = Signal()
    sigSendScanParameters = Signal(dict, dict, object)  # (analogParams, digitalParams, scannerList)
    sigSetAxisCenters = Signal(object, object)  # (axisDeviceList, axisCenterList)
    sigRequestScanFreq = Signal()
    sigSendScanFreq = Signal(float)  # (scanPeriod)
    # Deprecated compatibility signal.
    sigScanFrameFinished = Signal()  # TODO: emit this signal when a scanning frame finished, maybe in scanController if possible? Otherwise in APDManager for now, even if that is not general if you want to do camera-based experiments. Could also create a signal specifically for this from the scan curve generator perhaps, specifically for the rotation experiments, would that be smarter?

    # Snapshot and external recording trigger events.
    sigSnapImg = Signal()
    sigSnapImgPrev = Signal(str, np.ndarray, str)  # (detector, image, nameSuffix)
    sigStartRecordingExternal = Signal()

    # SLM events.
    sigSLMMaskUpdated = Signal(object)  # (mask)

    # Focus and rotation workflow events.
    sigSaveFocus = Signal()
    sigUpdateRotatorPosition = Signal(str)  # (rotatorName)
    sigSetSyncInMovementSettings = Signal(str, float, bool, bool)  # (rotatorName, position, relativeShift, enabled)

    # Event-triggered workflow events.
    sigInitiateEtMonalisa = Signal(bool)
    sigInitiateEt = Signal(bool)
    sigInitiateEtSnouty = Signal(bool)
    # Deprecated compatibility signal.
    sigClockWidefield = Signal()
    sigRunScanTriggerScopePLSRMulticolor = Signal()

    # Bead-recognition and MoNaLISA scan helper events.
    sigQueryCenterCoord = Signal(str)  # (search mode)
    sigCenterCoordPipelineFinished = Signal(object)  # (center coordinates or None)
    sigUpdateBeadRecCenter = Signal(int, int)  # (y, x) coordinates
    sigShowBeadRecCenterCross = Signal(bool)  # (state)
    sigAutoAxialToggled = Signal(bool)  # (state)
    sigNewAxialListBuffer = Signal(list)  # e.g. ["XZ", "YZ"]

    # useq-schema related events.
    sigSetXYPosition = Signal(float, float)
    sigSetZPosition = Signal(float)
    sigSetExposure = Signal(float)
    sigSetSpeed = Signal(float)

    # Scripting events.
    sigScriptExecutionFinished = Signal()

    @property
    def sharedAttrs(self):
        return self.__sharedAttrs

    def __init__(self, main, setupInfo):
        super().__init__()
        self.__main = main
        self.__sharedAttrs = SharedAttributes()
        self.__logger = initLogger(self)
        self._scriptExecution = False
        self._create_event_groups()
        self.scanWorkflow = ScanWorkflowService(self)
        self.beadRecWorkflow = BeadRecWorkflowService(self)
        self.__main._moduleCommChannel.sigExecutionFinished.connect(self.executionFinished)

    def _create_event_groups(self):
        """Create domain aliases while preserving legacy ``sigX`` attributes."""
        self.acquisitionEvents = pythontools.dictToROClass({
            'updateImage': self.sigUpdateImage,
            'acquisitionStarted': self.sigAcquisitionStarted,
            'acquisitionStopped': self.sigAcquisitionStopped,
            'adjustFrame': self.sigAdjustFrame,
            'detectorSwitched': self.sigDetectorSwitched,
            'newFrame': self.sigNewFrame,
        })
        self.viewEvents = pythontools.dictToROClass({
            'gridToggled': self.sigGridToggled,
            'crosshairToggled': self.sigCrosshairToggled,
            'addItemToVb': self.sigAddItemToVb,
            'removeItemFromVb': self.sigRemoveItemFromVb,
        })
        self.recordingEvents = pythontools.dictToROClass({
            'recordingStarted': self.sigRecordingStarted,
            'recordingEnded': self.sigRecordingEnded,
            'updateRecFrameNum': self.sigUpdateRecFrameNum,
            'updateRecTime': self.sigUpdateRecTime,
            'memorySnapAvailable': self.sigMemorySnapAvailable,
        })
        self.scanEvents = pythontools.dictToROClass({
            'runScan': self.sigRunScan,
            'abortScan': self.sigAbortScan,
            'scanStarting': self.sigScanStarting,
            'scanBuilt': self.sigScanBuilt,
            'scanStarted': self.sigScanStarted,
            'scanDone': self.sigScanDone,
            'scanEnded': self.sigScanEnded,
            'toggleBlockScanWidget': self.sigToggleBlockScanWidget,
            'requestScanParameters': self.sigRequestScanParameters,
            'sendScanParameters': self.sigSendScanParameters,
            'setAxisCenters': self.sigSetAxisCenters,
            'requestScanFreq': self.sigRequestScanFreq,
            'sendScanFreq': self.sigSendScanFreq,
            'scanFrameFinished': self.sigScanFrameFinished,
        })
        self.snapshotEvents = pythontools.dictToROClass({
            'snapImg': self.sigSnapImg,
            'snapImgPrev': self.sigSnapImgPrev,
            'startRecordingExternal': self.sigStartRecordingExternal,
        })
        self.slmEvents = pythontools.dictToROClass({
            'slmMaskUpdated': self.sigSLMMaskUpdated,
        })
        self.focusEvents = pythontools.dictToROClass({
            'saveFocus': self.sigSaveFocus,
        })
        self.rotationEvents = pythontools.dictToROClass({
            'updateRotatorPosition': self.sigUpdateRotatorPosition,
            'setSyncInMovementSettings': self.sigSetSyncInMovementSettings,
        })
        self.eventTriggeredEvents = pythontools.dictToROClass({
            'initiateEtMonalisa': self.sigInitiateEtMonalisa,
            'initiateEt': self.sigInitiateEt,
            'clockWidefield': self.sigClockWidefield,
        })
        self.beadRecEvents = pythontools.dictToROClass({
            'queryCenterCoord': self.sigQueryCenterCoord,
            'centerCoordPipelineFinished': self.sigCenterCoordPipelineFinished,
            'updateBeadRecCenter': self.sigUpdateBeadRecCenter,
            'showBeadRecCenterCross': self.sigShowBeadRecCenterCross,
            'autoAxialToggled': self.sigAutoAxialToggled,
            'newAxialListBuffer': self.sigNewAxialListBuffer,
        })
        self.useqEvents = pythontools.dictToROClass({
            'setXYPosition': self.sigSetXYPosition,
            'setZPosition': self.sigSetZPosition,
            'setExposure': self.sigSetExposure,
            'setSpeed': self.sigSetSpeed,
        })
        self.scriptEvents = pythontools.dictToROClass({
            'scriptExecutionFinished': self.sigScriptExecutionFinished,
        })

    def _get_required_controller(self, widgetKey, displayName=None):
        """Return a controller by widget key or raise the legacy RuntimeError."""
        try:
            return self.__main.controllers[widgetKey]
        except KeyError:
            name = displayName or widgetKey.lower()
            raise RuntimeError(f'Required {name} widget not available') from None

    def isScanRunning(self) -> bool:
        """
        Returns whether a scan is ongoing or not.
        """
        return self._get_required_controller('Scan', 'scan').isRunning

    def getCenterViewbox(self):
        """ Returns the center point of the viewbox, as an (x, y) tuple. """
        return self._get_required_controller('Image', 'image').getCenterViewbox()

    def getNumCamTTL(self):
        return self._get_required_controller('Scan', 'scan').getNumCamTTL()

    def getDimsScan(self):
        return self._get_required_controller('Scan', 'scan').getDimsScan()

    def getScanStepSizes(self):
        return self._get_required_controller('Scan', 'scan').getScanStepSizes()

    def getNumScanPositions(self):
        return self._get_required_controller('Scan', 'scan').getNumScanPositions()

    def getNextAxial(self):
        return self._get_required_controller('Scan', 'scan').getNextAxial()

    def get_image(self, detectorName=None):
        return self._get_required_controller('View', 'view').get_image(detectorName)

    @APIExport(runOnUIThread=True)
    def acquireImage(self) -> None:
        image = self.get_image()
        self.output.append(image)

    def runScript(self, text):
        self.output = []
        self._scriptExecution = True
        self.__main._moduleCommChannel.sigRunScript.emit(text)

    def executionFinished(self):
        self.sigScriptExecutionFinished.emit()
        self._scriptExecution = False

    def isExecuting(self):
        return self._scriptExecution

    @APIExport()
    def signals(self) -> Mapping[str, Signal]:
        """ Returns signals that can be used with e.g. the getWaitForSignal
        action. Currently available signals are:

         - acquisitionStarted
         - acquisitionStopped
         - recordingStarted
         - recordingEnded
         - scanEnded

        They can be accessed like this: api.imcontrol.signals().scanEnded
        """

        return pythontools.dictToROClass({
            'acquisitionStarted': self.sigAcquisitionStarted,
            'acquisitionStopped': self.sigAcquisitionStopped,
            'recordingStarted': self.sigRecordingStarted,
            'recordingEnded': self.sigRecordingEnded,
            'scanEnded': self.sigScanEnded,
            'saveFocus': self.sigSaveFocus
        })


# Copyright (C) 2020-2022 ImSwitch developers
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
