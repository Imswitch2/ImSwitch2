from typing import Mapping, TYPE_CHECKING

import numpy as np
from imswitch.imcommon.framework import Signal, SignalInterface
from imswitch.imcommon.model import pythontools, APIExport, SharedAttributes
from imswitch.imcommon.model import initLogger
from .WorkflowServices import BeadRecWorkflowService, ScanWorkflowService

if TYPE_CHECKING:
    from .controllers._beadrec_scan_source import BeadRecScanSource


#: Methods a controller must expose to be driven by a scan recording: the
#: targeted runner, its abort, and the geometry the recording is armed with.
RECORDING_SCAN_SOURCE_METHODS = (
    'runScanExternal',
    'abortScan',
    'getNumScanPositions',
    'getNumCamTTL',
)


#: Methods a controller must expose merely to be *run* once by a workflow.
#: Strictly weaker than RECORDING_SCAN_SOURCE_METHODS, which additionally
#: demands the scan geometry a recording must be armed with. A consumer that
#: only needs "run one scan, then stop" — tiling, for one — would wrongly
#: exclude a capable controller by testing the recording set.
SCAN_SOURCE_METHODS = (
    'runScanExternal',
    'abortScan',
)


def _scanSourceMatches(controllers, methods):
    return [
        (key, controller)
        for key, controller in controllers.items()
        if controller is not None and all(
            callable(getattr(controller, methodName, None))
            for methodName in methods
        )
    ]


def _recordingScanSourceMatches(controllers):
    """``[(key, controller)]`` for every controller a recording could drive."""
    return _scanSourceMatches(controllers, RECORDING_SCAN_SOURCE_METHODS)


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
    sigRecordingFailed = Signal(str)
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
    # (deviceList) — the devices the imminent scan will drive, published before
    # its execution backend starts. Consumers that must change hardware in
    # response to scan membership (laser arming) have to use this, not
    # sigScanBuilt: NI-DAQ publishes that after claiming the manager, while
    # autonomous firmware backends use it only as a compatibility boundary.
    sigScanDevicesResolved = Signal(object)
    # (positionerNameList) — the positioners the imminent run will drive,
    # published right after the run reservation and therefore before the scan
    # writes to any of them. sigScanDevicesResolved cannot serve this purpose:
    # it carries TTL-programmed device names only, so no positioner ever
    # appears in it. Consumers that must yield an actuator to the scan (the
    # focus lock, above all) suspend on sigScanStarting and use this to decide
    # whether the suspension was actually necessary. Absence means "unknown",
    # never "nothing" — a consumer that never receives it must stay suspended.
    sigScanActuatorsResolved = Signal(object)
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
        self._activeScanSource = None
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
            'recordingFailed': self.sigRecordingFailed,
            'updateRecFrameNum': self.sigUpdateRecFrameNum,
            'updateRecTime': self.sigUpdateRecTime,
            'memorySnapAvailable': self.sigMemorySnapAvailable,
        })
        self.scanEvents = pythontools.dictToROClass({
            'runScan': self.sigRunScan,
            'abortScan': self.sigAbortScan,
            'scanStarting': self.sigScanStarting,
            'scanDevicesResolved': self.sigScanDevicesResolved,
            'scanActuatorsResolved': self.sigScanActuatorsResolved,
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

    def controllerRegistry(self) -> Mapping:
        """The live widget-key -> controller mapping (empty before setup)."""
        return getattr(self.__main, 'controllers', None) or {}

    def _get_required_controller(self, widgetKey, displayName=None):
        """Return a controller by widget key or raise the legacy RuntimeError."""
        try:
            return self.__main.controllers[widgetKey]
        except KeyError:
            name = displayName or widgetKey.lower()
            raise RuntimeError(f'Required {name} widget not available') from None

    def setActiveScanSource(self, controller) -> None:
        """ Announce the controller that is starting a scan as the active scan
        source. Called from ScanLifecycleMixin when a scan controller sets its
        isRunning flag to True; the last announcement wins. """
        self._activeScanSource = controller

    def clearActiveScanSource(self, controller) -> None:
        """ Withdraw a controller as the active scan source. Identity-guarded:
        only clears if the given controller is the active source, so a
        controller initializing its isRunning flag to False cannot clear
        another controller's ongoing scan. """
        if self._activeScanSource is controller:
            self._activeScanSource = None

    def getActiveScanSource(self):
        """ Return the controller currently running a scan, or None. """
        return self._activeScanSource

    def isScanRunning(self) -> bool:
        """
        Returns whether a scan is ongoing or not, based on the active scan
        source announced by the scan controller that started the scan (see
        ScanLifecycleMixin).
        """
        return self._activeScanSource is not None

    def getCenterViewbox(self):
        """ Returns the center point of the viewbox, as an (x, y) tuple. """
        return self._get_required_controller('Image', 'image').getCenterViewbox()

    def hasScanWidget(self) -> bool:
        """Return whether a generic 'Scan' widget is registered in this setup."""
        controllers = getattr(self.__main, 'controllers', None) or {}
        return 'Scan' in controllers

    def getRecordingScanSourceNames(self):
        """Widget keys of every controller a scan recording could drive.

        The Recording widget offers these when more than one exists; a rig with
        a single scanner needs no choice and is never asked to make one.
        """
        controllers = getattr(self.__main, 'controllers', None) or {}
        return [
            key
            for key, _controller in _recordingScanSourceMatches(controllers)
        ]

    def getRecordingFolder(self):
        """The output folder the Recording widget is currently pointed at.

        The single place the operator sets "where my data goes", so anything
        else that writes files should start from here rather than invent its
        own root. Returns None when there is no Recording widget, leaving the
        caller to fall back.
        """
        controllers = getattr(self.__main, 'controllers', None) or {}
        controller = controllers.get('Recording')
        getter = getattr(controller, 'getRecFolder', None)
        if not callable(getter):
            return None
        try:
            folder = getter()
        except Exception:
            return None
        return str(folder) if folder else None

    def getScanSourceNames(self):
        """Widget keys of every controller a workflow could run one scan on.

        Wider than :meth:`getRecordingScanSourceNames` — it does not require
        the scan-geometry accessors a recording needs.
        """
        controllers = getattr(self.__main, 'controllers', None) or {}
        return [
            key
            for key, _controller in _scanSourceMatches(
                controllers, SCAN_SOURCE_METHODS
            )
        ]

    def getScanSource(self, preferredKey=None):
        """Resolve one controller to run a single scan on.

        Same safety rule as :meth:`getRecordingScanSource`: an explicit choice
        wins, then the canonical ``Scan`` controller, then a lone capable
        controller. Several capable controllers with no choice made raises,
        because broadcasting to all of them would command hardware on
        scanners the operator did not select.
        """
        controllers = getattr(self.__main, 'controllers', None) or {}
        matches = _scanSourceMatches(controllers, SCAN_SOURCE_METHODS)

        if preferredKey:
            for key, controller in matches:
                if key == preferredKey:
                    return controller
            raise RuntimeError(
                f'The selected scan source "{preferredKey}" is not available '
                'or no longer exposes runScanExternal. Pick another scan '
                'source.'
            )

        for key, controller in matches:
            if key == 'Scan':
                return controller

        if len(matches) == 1:
            return matches[0][1]

        candidates = [key for key, _controller in matches]
        if candidates:
            raise RuntimeError(
                'Several scan controllers are registered '
                f'({candidates}); select which one to trigger.'
            )
        raise RuntimeError(
            'No controller exposes runScanExternal, so no scan can be '
            'triggered on this setup.'
        )

    def getRecordingScanSource(self, preferredKey=None):
        """Resolve one controller that can safely drive scan recording.

        The global ``sigRunScan`` broadcast is unsafe on standalone
        TriggerScope/LightSheet setups because every scan controller receives
        it and can command hardware. Resolution order: the operator's explicit
        choice, then the canonical Scan controller, then a lone standalone
        controller. Several capable controllers with no choice made is an
        ambiguity, not a default — guessing there is what binds a recording to
        the wrong scanner's geometry.
        """
        controllers = getattr(self.__main, 'controllers', None) or {}
        matches = _recordingScanSourceMatches(controllers)
        if preferredKey:
            for key, controller in matches:
                if key == preferredKey:
                    return controller
            raise RuntimeError(
                f'The selected scan source "{preferredKey}" is not available '
                'or no longer exposes the recording accessors. Pick another '
                'scan source in the Recording widget.'
            )

        for key, controller in matches:
            if key == 'Scan':
                return controller

        if len(matches) == 1:
            return matches[0][1]

        candidates = [key for key, _controller in matches]
        if candidates:
            detail = f'multiple capable controllers are registered: {candidates}'
        else:
            detail = 'no controller exposes the required recording accessors'
        raise RuntimeError(
            'Cannot automate scan-lapse recording safely: '
            f'{detail}. Select/configure one recording-capable scan source.'
        )

    def _resolveScanAccessor(self, methodName):
        """Return a bound scan accessor, resolved in priority order:

        1. The active scan source (the controller currently running a scan).
        2. The legacy 'Scan' widget controller.
        3. While idle on setups without a 'Scan' widget: the unique
           registered controller exposing the accessor. If several expose
           it the resolution is ambiguous — warn and give up rather than
           guess by registration order.

        Returns None when nothing resolves.
        """
        source = self._activeScanSource
        if source is not None:
            getter = getattr(source, methodName, None)
            if getter is not None:
                return getter
        try:
            controller = self._get_required_controller('Scan', 'scan')
        except RuntimeError:
            controllers = getattr(self.__main, 'controllers', None) or {}
            matches = [
                (key, getattr(c, methodName))
                for key, c in controllers.items() if hasattr(c, methodName)
            ]
            if len(matches) == 1:
                return matches[0][1]
            if len(matches) > 1:
                self.__logger.warning(
                    f'Cannot resolve {methodName} while no scan is running: '
                    f'multiple controllers expose it '
                    f'({[key for key, _ in matches]}).'
                )
            return None
        return getattr(controller, methodName, None)

    def getNumCamTTL(self):
        getter = self._resolveScanAccessor('getNumCamTTL')
        if getter is None:
            raise RuntimeError('No scan controller available to provide getNumCamTTL')
        return getter()

    def _asBeadRecScanSource(self, controller) -> 'BeadRecScanSource | None':
        """Return controller as a compatible BeadRec scan source, else None."""
        from .controllers._beadrec_scan_source import BeadRecScanSource
        if controller is None:
            return None
        if (
            isinstance(controller, BeadRecScanSource)
            or (
                hasattr(controller, 'getBeadRecScanDims')
                and hasattr(controller, 'getBeadRecStepSizes')
                and hasattr(controller, 'getNumLineSteps')
                and hasattr(controller, 'getFramesPerScanPixel')
                and hasattr(controller, 'isBeadRecCompatible')
            )
        ):
            if controller.isBeadRecCompatible():
                return controller
        return None

    def _activeBeadRecScanSource(self) -> 'BeadRecScanSource | None':
        """Return the active scan source if it satisfies the BeadRecScanSource
        protocol and reports itself BeadRec-compatible, else None."""
        return self._asBeadRecScanSource(self._activeScanSource)

    def getBeadRecScanSource(self) -> 'BeadRecScanSource | None':
        """Return the BeadRec-compatible scan source, or None.

        The active scan source (the controller currently running a scan) takes
        precedence. When no scan is running, falls back to iterating all
        registered controllers and returning the first one that:
        1. Implements the BeadRecScanSource protocol methods
        2. Returns True from isBeadRecCompatible()
        """
        active = self._activeBeadRecScanSource()
        if active is not None:
            return active
        controllers = getattr(self.__main, 'controllers', None)
        if controllers is None:
            return None
        for controller in controllers.values():
            source = self._asBeadRecScanSource(controller)
            if source is not None:
                return source
        return None

    def getDimsScan(self):
        source = self._activeBeadRecScanSource()
        if source is not None:
            dims = source.getBeadRecScanDims()
            return [dims[0], dims[1]]
        try:
            controller = self._get_required_controller('Scan', 'scan')
        except RuntimeError:
            source = self.getBeadRecScanSource()
            if source is not None:
                dims = source.getBeadRecScanDims()
                return [dims[0], dims[1]]
            raise
        source = self._asBeadRecScanSource(controller)
        if source is not None:
            dims = source.getBeadRecScanDims()
            return [dims[0], dims[1]]
        getter = getattr(controller, 'getDimsScan', None)
        if getter is not None:
            return getter()
        raise RuntimeError('No scan controller available to provide getDimsScan')

    def getScanStepSizes(self):
        source = self._activeBeadRecScanSource()
        if source is not None:
            steps = source.getBeadRecStepSizes()
            return [steps[0], steps[1]]
        try:
            controller = self._get_required_controller('Scan', 'scan')
        except RuntimeError:
            source = self.getBeadRecScanSource()
            if source is not None:
                steps = source.getBeadRecStepSizes()
                return [steps[0], steps[1]]
            raise
        source = self._asBeadRecScanSource(controller)
        if source is not None:
            steps = source.getBeadRecStepSizes()
            return [steps[0], steps[1]]
        getter = getattr(controller, 'getScanStepSizes', None)
        if getter is not None:
            return getter()
        raise RuntimeError('No scan controller available to provide getScanStepSizes')

    def getNumLineSteps(self):
        """Return the number of linesteps from the scan controller. Returns 1
        if no scan controller is available or it does not expose the value
        (e.g. MoNaLISA/PointScan controllers without linestep support)."""
        source = self._activeBeadRecScanSource()
        if source is not None:
            return source.getNumLineSteps()
        try:
            controller = self._get_required_controller('Scan', 'scan')
        except RuntimeError:
            source = self.getBeadRecScanSource()
            if source is not None:
                return source.getNumLineSteps()
            return 1
        source = self._asBeadRecScanSource(controller)
        if source is not None:
            return source.getNumLineSteps()
        getter = getattr(controller, 'getNumLineSteps', None)
        return getter() if getter is not None else 1

    def getFramesPerScanPixel(self):
        """Return the number of detector frames produced per physical scan
        pixel (e.g. the count of camera-enabled linesteps in advanced scans).
        Returns 1 if the scan controller does not expose the value."""
        source = self._activeBeadRecScanSource()
        if source is not None:
            return source.getFramesPerScanPixel()
        try:
            controller = self._get_required_controller('Scan', 'scan')
        except RuntimeError:
            source = self.getBeadRecScanSource()
            if source is not None:
                return source.getFramesPerScanPixel()
            return 1
        source = self._asBeadRecScanSource(controller)
        if source is not None:
            return source.getFramesPerScanPixel()
        getter = getattr(controller, 'getFramesPerScanPixel', None)
        return getter() if getter is not None else 1

    def getNumScanPositions(self):
        getter = self._resolveScanAccessor('getNumScanPositions')
        if getter is None:
            raise RuntimeError('No scan controller available to provide getNumScanPositions')
        return getter()

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
         - recordingFailed
         - scanEnded

        They can be accessed like this: api.imcontrol.signals().scanEnded
        """

        return pythontools.dictToROClass({
            'acquisitionStarted': self.sigAcquisitionStarted,
            'acquisitionStopped': self.sigAcquisitionStopped,
            'recordingStarted': self.sigRecordingStarted,
            'recordingEnded': self.sigRecordingEnded,
            'recordingFailed': self.sigRecordingFailed,
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
