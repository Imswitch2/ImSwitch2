import copy
import functools
import json
import os
import threading
import traceback

from abc import abstractmethod

from qtpy import QtCore

from imswitch.imcommon.framework import Signal
from imswitch.imcommon.controller.basecontrollers import (
    WidgetController,
    WidgetControllerFactory,
)
from imswitch.imcontrol.model import InvalidChildClassError
from imswitch.imcontrol.model.managers._scan_execution import (
    FINISH_ABORT, FINISH_GRACEFUL, getSharedScanExecutionCoordinator,
)
from imswitch.imcontrol.controller.WorkflowServices import (
    ScanRequestCompletion,
)
from imswitch.imcontrol.model.state_contracts import ComponentStateApplyMode
from imswitch.imcommon.model import APIExport, dirtools, initLogger

# ``ComponentStateApplyMode`` is re-exported above (defined in model.state_contracts)
# so existing ``from ..basecontrollers import ComponentStateApplyMode`` imports keep
# working while the canonical definition lives in the model layer.


class ImConWidgetControllerFactory(WidgetControllerFactory):
    """ Factory class for creating a ImConWidgetController object. """

    def __init__(self, setupInfo, master, commChannel, moduleCommChannel):
        super().__init__(setupInfo=setupInfo, master=master, commChannel=commChannel,
                         moduleCommChannel=moduleCommChannel)


class ImConWidgetController(WidgetController):
    """ Superclass for all ImConWidgetController.
    All WidgetControllers should have access to the setup information,
    MasterController, CommunicationChannel and the linked Widget. """

    sigInvokeOnControllerThread = Signal(object)

    def __init__(self, setupInfo, commChannel, master, *args, **kwargs):
        # Protected attributes, which should only be accessed from controller and its subclasses
        self._setupInfo = setupInfo
        self._commChannel = commChannel
        self._master = master

        # Init superclass
        super().__init__(*args, **kwargs)
        queuedConnection = getattr(
            QtCore.Qt, 'ConnectionType', QtCore.Qt
        ).QueuedConnection
        try:
            self.sigInvokeOnControllerThread.connect(
                self.__invokeOnControllerThread,
                type=queuedConnection,
            )
        except TypeError:
            self.sigInvokeOnControllerThread.connect(
                self.__invokeOnControllerThread,
                queuedConnection,
            )

    def _invokeOnControllerThread(self, callback) -> None:
        """Queue ``callback`` onto this controller QObject's affinity thread."""
        self.sigInvokeOnControllerThread.emit(callback)

    def _invokeOnControllerThreadIfNeeded(self, callback) -> None:
        """Run now on the affinity thread, otherwise queue to that thread."""
        try:
            if QtCore.QThread.currentThread() is self.thread():
                callback()
                return
        except Exception:
            # Lightweight non-Qt test adapters execute synchronously.
            callback()
            return
        self._invokeOnControllerThread(callback)

    def __invokeOnControllerThread(self, callback) -> None:
        try:
            callback()
        except Exception:
            self._logger.error(
                'A controller-thread lifecycle handoff failed',
                exc_info=True,
            )


class SetupModeApplyPriority:
    """Declarative ordering bands for setup-mode application.

    Components should set ``setupModeApplyPriority`` to one of these bands
    instead of adding their component name to ``SetupModeController``. Lower
    numbers apply first; components with the same priority keep the mode file's
    component order.
    """

    DETECTOR_SETTINGS = 100
    SCAN = 200
    MULTI_SPATIAL_LIGHT_MODULATOR = 300
    SPATIAL_LIGHT_MODULATOR = 310
    MICROSCOPE_STAND = 400
    BEAM_PATH = 410
    EXCITATION = 500
    DEFAULT = 1000


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
    setupModeDisplayName: str | None = None
    legacyStateNames: tuple = ()
    setupModeCategory: str = 'default'
    setupModeApplyPriority: int = SetupModeApplyPriority.DEFAULT
    setupModeHardwareCritical: bool = False
    
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
    scan controller inherits this mixin. See docs/scan-lifecycle.rst.
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


    def _resolveScanActuators(self):
        """Positioners this run will drive, or None when that is not knowable.

        Both sets count. ``_positionersScan`` are the axes the waveform sweeps;
        ``target_device`` additionally covers scanners that are merely parked
        at their centre position before arming, which is still a hardware move
        on that actuator.

        Returning None rather than an empty list matters: this runs before
        ``getParameters``, so the parameter dict can legitimately be empty on a
        very first start. A consumer yielding hardware to the scan must read
        that as "unknown" and stay yielded, never as "the scan drives nothing".
        """
        analogParameterDict = getattr(self, '_analogParameterDict', None) or {}
        scannedPositioners = getattr(self, '_positionersScan', None) or []
        targetDevices = analogParameterDict.get('target_device') or []
        if not targetDevices and not scannedPositioners:
            return None

        actuators = set()
        for name in list(scannedPositioners) + list(targetDevices):
            if name and str(name) != 'None':
                actuators.add(str(name))
        return sorted(actuators)

    def _publishScanActuators(self):
        """Announce the positioners this run owns, before it writes to them.

        A failure to resolve must not block the scan: the scan does not depend
        on this publication, only its consumers do. Staying silent leaves them
        on their safe default.
        """
        try:
            actuators = self._resolveScanActuators()
        except Exception:
            self._logger.error(
                'Could not resolve the scan actuator list; consumers that '
                f'yield hardware to the scan are not being notified:\n'
                f'{traceback.format_exc()}'
            )
            return
        if actuators is None:
            return
        try:
            self.emitScanSignal(
                self._commChannel.sigScanActuatorsResolved, actuators
            )
        except Exception:
            self._logger.error(
                'A scan-actuator listener failed', exc_info=True
            )


class SuperScanController(StatefulComponentMixin, ScanLifecycleMixin, ImConWidgetController):
    componentName = 'Scan'
    supportsExactScanRequestCompletion = True
    stateSchemaVersion = 1
    legacyStateNames = ('ScanController', 'ScanControllerAdvanced', 'ScanControllerMoNaLISA', 'ScanControllerPointScan')
    setupModeCategory = 'scan'
    setupModeApplyPriority = SetupModeApplyPriority.SCAN

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
        # True between a repeat-scan frame finishing and the next frame arming.
        # See _armRepeatScan for why the re-arm is deferred.
        self._repeatPending = False
        # Run-level ownership deliberately outlives each NI-DAQ iteration. It
        # spans repeat gaps, non-final sequence parts and MoNaLISA axial
        # follow-ups, and is released exactly once with sigScanEnded.
        self._scanRunToken = None
        self._scanRunStartingPublished = False
        self._scanStopRequested = False
        self._scanRunFailed = False
        self._scanClosing = False
        # Per-broadcast acknowledgement state. Recording uses this synchronous
        # result to distinguish a scan that reserved/armed from a legacy signal
        # that was emitted but refused by every controller.
        self._externalScanRequestInProgress = False
        self._externalScanRequestAccepted = False
        self._externalScanRequestFailed = False
        self._externalScanRequestFailureMessage = ''
        self._externalScanRequestRunToken = None
        self._externalScanRequestCompletion = None
        self._pendingExternalScanRequestCompletions = []
        self._scanCompletionPublishing = False
        # Terminal ownership is claimed atomically before local run identity is
        # cleared. Close, failure and completion paths can otherwise race,
        # capture the same token, and publish the same run-level end twice.
        self._scanRunTerminalLock = threading.RLock()

        self.positioners = {
            pName: pManager for pName, pManager in self._setupInfo.positioners.items()
            if pManager.forScanning
        }
        self.TTLDevices = self._setupInfo.getTTLDevices()

        self.scanDir = os.path.join(dirtools.UserFileDirs.Root, 'imcontrol_scans')
        if not os.path.exists(self.scanDir):
            os.makedirs(self.scanDir)

        # All NI-DAQ entry points share one global iteration owner. Ownership is
        # recorded on the token, so the broadcast completion signals are acted
        # on only by the controller that armed the scan.
        self._scanCoordinator = getSharedScanExecutionCoordinator(
            self._master.detectorsManager,
            self._master.nidaqManager,
            logger=self._logger,
            scheduleTimeout=lambda delayS, callback: QtCore.QTimer.singleShot(
                int(delayS * 1000), callback
            ),
        )

        # Connect NidaqManager signals
        self._master.nidaqManager.sigScanBuilt.connect(
            lambda _, __, deviceList: self.emitScanSignal(self._commChannel.sigScanBuilt, deviceList)
        )
        self._master.nidaqManager.sigScanStarted.connect(
            lambda: self.emitScanSignal(self._commChannel.sigScanStarted)
        )
        # NI-DAQ is authoritative: abortScan() does not stop a running scan, so
        # ownership is released on sigScanDone rather than at the user's abort
        # click.
        self._master.nidaqManager.sigScanDone.connect(self.__onNidaqScanDone)
        self._master.nidaqManager.sigScanBuildFailed.connect(
            self.__onNidaqScanBuildFailed
        )

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
        requestCompletion = ScanRequestCompletion(self)
        self._externalScanRequestInProgress = True
        self._externalScanRequestAccepted = False
        self._externalScanRequestFailed = False
        self._externalScanRequestFailureMessage = ''
        self._externalScanRequestRunToken = None
        self._externalScanRequestCompletion = requestCompletion
        try:
            refusalMessage = SuperScanController._externalScanStartRefusal(
                self
            )
            if refusalMessage:
                self._externalScanRequestFailureMessage = refusalMessage
                return
            self._widget.setScanMode()
            self._widget.setRepeatEnabled(False)
            self.runScanAdvanced(
                recalculateSignals=recalculateSignals,
                isNonFinalPartOfSequence=isNonFinalPartOfSequence,
                sigScanStartingEmitted=True,
            )
        except Exception as error:
            self._externalScanRequestFailed = True
            self._externalScanRequestFailureMessage = str(error)
            coordinator = getattr(self, '_scanCoordinator', None)
            if coordinator is not None:
                ownedRun = coordinator.runForOwner(self)
                activeIteration = coordinator.tokenForOwner(self)
                if ownedRun is not None and activeIteration is None:
                    # A continuation can fail in setScanMode/setRepeatEnabled
                    # before _beginScanRun binds this request. Its earlier
                    # non-final part still owns one published lifecycle, so
                    # terminalize that idle retained run instead of leaking it.
                    try:
                        self.scanFailed()
                    except Exception:
                        self._logger.error(
                            'Failed to terminalize an idle scan continuation',
                            exc_info=True,
                        )
                        self._finishScanRun()
            raise
        finally:
            # Acceptance means this request obtained an exact run identity, not
            # that arming later completed successfully.  A controller can bind
            # the request and then fail while its detector-finish barrier is
            # still draining.  Reporting that case as an unowned rejection
            # would let the caller synthesize an early terminal and would
            # resolve the exact completion before the run is really released.
            accepted = (
                self._externalScanRequestAccepted
                and requestCompletion.runToken is not None
            )
            message = self._externalScanRequestFailureMessage
            if not accepted and not message:
                message = (
                    'Scan controller refused the request or failed to arm.'
                )
            reportResult = getattr(
                self._commChannel.scanWorkflow,
                'report_scan_request_result',
                None,
            )
            try:
                if callable(reportResult):
                    reportResult(
                        self,
                        accepted,
                        message,
                        getattr(
                            self, '_externalScanRequestRunToken', None
                        )
                        if accepted else None,
                        requestCompletion if accepted else None,
                    )
            finally:
                if not accepted:
                    runToken = requestCompletion.runToken
                    if runToken is not None:
                        requestCompletion.resolve(
                            runToken,
                            False,
                            message,
                        )
                    pending = self.__dict__.setdefault(
                        '_pendingExternalScanRequestCompletions', []
                    )
                    if requestCompletion in pending:
                        pending.remove(requestCompletion)
                self._externalScanRequestInProgress = False
                self._externalScanRequestRunToken = None
                self._externalScanRequestCompletion = None

    def _externalScanStartRefusal(self) -> str:
        """Return a side-effect-free refusal reason, or ``''`` if safe to arm.

        External requests configure widget scan/repeat state before concrete
        controllers enter ``_beginScanRun``. Ownership therefore has to be
        preflighted here; otherwise a duplicate request can disable Repeat on
        the acquisition it is about to reject. A same-owner run with no active
        iteration is the intentional non-final/repeat-gap continuation.
        """
        if bool(getattr(self, '_scanCompletionPublishing', False)):
            return (
                'The previous scan completion is still being published.'
            )
        if bool(getattr(self, 'isRunning', False)):
            return 'This scan controller already has an active iteration.'

        coordinator = getattr(self, '_scanCoordinator', None)
        if coordinator is None:
            return ''
        try:
            activeIteration = getattr(coordinator, 'activeToken')
        except Exception:
            tokenForOwner = getattr(coordinator, 'tokenForOwner', None)
            try:
                activeIteration = (
                    tokenForOwner(self)
                    if callable(tokenForOwner) else None
                )
            except Exception:
                return 'Unable to verify current scan-iteration ownership.'
        if activeIteration is not None:
            return 'A scan iteration is already active or still finishing.'

        try:
            activeRun = getattr(coordinator, 'activeRunToken')
        except Exception:
            runForOwner = getattr(coordinator, 'runForOwner', None)
            try:
                activeRun = (
                    runForOwner(self)
                    if callable(runForOwner) else None
                )
            except Exception:
                return 'Unable to verify current scan-run ownership.'
        if activeRun is None:
            return ''

        localToken = getattr(self, '_scanRunToken', None)
        if activeRun is not localToken:
            return 'Another or mismatched scan run is already reserved.'
        if (
            bool(getattr(self, '_repeatPending', False))
            or bool(getattr(self, 'awaitingPipeline', False))
        ):
            return 'The current scan run already has an internal continuation.'
        for completion in tuple(getattr(
            self, '_pendingExternalScanRequestCompletions', ()
        ) or ()):
            if getattr(completion, 'runToken', None) is not activeRun:
                continue
            wait = getattr(completion, 'wait', None)
            try:
                unresolved = not callable(wait) or not wait(timeout=0)
            except Exception:
                unresolved = True
            if unresolved:
                return (
                    'The current scan run already has an unresolved external '
                    'request.'
                )
        if (
            bool(getattr(self, '_scanRunFailed', False))
            or bool(getattr(self, '_scanStopRequested', False))
            or bool(getattr(activeRun, 'releaseRequested', False))
        ):
            return 'The current scan run is failed, stopped, or releasing.'
        return ''

    def _detachExternalScanRequestCompletions(self, runToken):
        """Remove and return the requests pending for this exact run.

        Detaching before a compatibility signal is emitted prevents a
        re-entrant continuation from being swept into the previous part's
        completion merely because both parts intentionally share a run token.
        """
        if runToken is None:
            return ()
        pending = self.__dict__.setdefault(
            '_pendingExternalScanRequestCompletions', []
        )
        matched = tuple(
            completion for completion in pending
            if completion.runToken is runToken
        )
        if matched:
            pending[:] = [
                completion for completion in pending
                if completion not in matched
            ]
        return matched

    def _completeExternalScanRequest(
        self, runToken, *, successful: bool, message: str = '',
        completions=None,
    ) -> bool:
        """Resolve request terminals detached for this exact scan part."""
        if runToken is None:
            return False
        if completions is None:
            completions = self._detachExternalScanRequestCompletions(
                runToken
            )
        resolved = False
        for completion in tuple(completions):
            if completion.resolve(runToken, successful, message):
                resolved = True
        return resolved

    def _requeueExternalScanRequestCompletions(
        self, runToken, completions
    ) -> None:
        """Restore unresolved terminals after an unproven run release.

        Final success/failure callbacks are detached before compatibility
        signals are emitted to make signal re-entry identity-safe. If the
        coordinator then cannot prove release, those terminals must be put back
        so a later abort/close retry can resolve them after the real barrier.
        """
        if runToken is None:
            return
        pending = self.__dict__.setdefault(
            '_pendingExternalScanRequestCompletions', []
        )
        for completion in tuple(completions):
            if getattr(completion, 'runToken', None) is not runToken:
                continue
            wait = getattr(completion, 'wait', None)
            if callable(wait) and wait(timeout=0):
                continue
            if completion not in pending:
                pending.append(completion)

    def _publishScanDone(self, *, isFinalPart: bool) -> None:
        """Publish success for this controller's exact external request."""
        runToken = getattr(self, '_scanRunToken', None)
        completions = self._detachExternalScanRequestCompletions(runToken)
        self._scanCompletionPublishing = True
        signalError = None
        try:
            self.emitScanSignal(self._commChannel.sigScanDone)
        except Exception as error:
            signalError = error
            self._logger.error(
                'A scan-completion listener failed',
                exc_info=True,
            )
        finally:
            completionLock = threading.Lock()
            completionPublished = False

            def completeRequest(finishError=None):
                nonlocal completionPublished
                with completionLock:
                    if completionPublished:
                        return
                    completionPublished = True
                if (
                    finishError is not None
                    and not SuperScanController._scanRunReleaseProven(
                        self, runToken
                    )
                ):
                    # A held reservation whose finalization failed is not an
                    # exact terminal yet. Keep the request attached to this
                    # identity so abort/timeout/close can retry finalization;
                    # waking it now would only make its next start hit Busy.
                    self._scanRunFailed = True
                    if not getattr(
                        self, '_externalScanRequestFailureMessage', ''
                    ):
                        self._externalScanRequestFailureMessage = (
                            'Failed to finalize the scan-run lifecycle.'
                        )
                    self._requeueExternalScanRequestCompletions(
                        runToken, completions
                    )
                    self._scanCompletionPublishing = False
                    return
                aborted = bool(
                    getattr(self, '_scanStopRequested', False)
                )
                failed = bool(getattr(self, '_scanRunFailed', False))
                terminalError = signalError or finishError
                # Clear the re-entry gate before waking completion waiters or
                # invoking callbacks. A worker can otherwise wake, dispatch the
                # next scan on the UI thread, and be spuriously refused while
                # this terminal is already physically complete.
                self._scanCompletionPublishing = False
                self._completeExternalScanRequest(
                    runToken,
                    successful=(
                        not aborted
                        and not failed
                        and terminalError is None
                    ),
                    message=(
                        'Scan was aborted.'
                        if aborted else
                        (
                            getattr(
                                self,
                                '_externalScanRequestFailureMessage',
                                '',
                            )
                            or 'Scan failed before successful completion.'
                        )
                        if failed else
                        'A scan-completion listener failed.'
                        if signalError is not None else
                        'Failed to finalize the scan-run lifecycle.'
                        if finishError is not None else ''
                    ),
                    completions=completions,
                )

            mustFinalizeRun = (
                isFinalPart
                or signalError is not None
                or bool(getattr(self, '_scanStopRequested', False))
                or bool(getattr(self, '_scanRunFailed', False))
            )
            if mustFinalizeRun:
                try:
                    # The exact request terminal belongs after the run release
                    # callback.  releaseRun may defer that callback until every
                    # detector has acknowledged its final-frame barrier.
                    self._finishScanRun(
                        onReleased=completeRequest,
                        expectedRunToken=runToken,
                    )
                except Exception:
                    self._logger.error(
                        'Failed to finalize the scan-run lifecycle',
                        exc_info=True,
                    )
                    # Make one failure-path retry while retaining the outer
                    # publication gate and the captured run identity. If the
                    # coordinator still cannot prove release, scanFailed
                    # restores the exact terminal for an abort/close retry.
                    self.scanFailed(
                        _terminalRunToken=runToken,
                        _terminalCompletions=completions,
                        _forceFinalizeDuringPublication=True,
                    )
            else:
                # A non-final sequence part deliberately retains the run-level
                # reservation. Its exact part terminal is nevertheless ready
                # once sigScanDone publication has returned.
                completeRequest()
    
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
    def saveScanParamsToFile(self, filePath: str) -> None:
        """ Saves the set scanning parameters to the specified file. """
        if not filePath.endswith('.json'):
            filePath += '.json'
        state = self.getComponentState()
        try:
            with open(filePath, 'w') as f:
                json.dump(state, f, indent=2)
            self._logger.info(f'Scan parameters saved to {filePath}')
        except Exception:
            self._logger.error(f'Failed to save scan parameters:\n{traceback.format_exc()}')

    @APIExport(runOnUIThread=True)
    def loadScanParamsFromFile(self, filePath: str) -> None:
        """ Loads scanning parameters from the specified file. """
        payload = self._read_scan_file(filePath)
        if payload is None:
            return
        warnings = self.applyComponentState(payload, applyMode=ComponentStateApplyMode.SETUP_MODE_APPLY)
        if warnings:
            for warning in warnings:
                self._logger.warning(warning)

    def _read_scan_file(self, filePath: str):
        """Read a scan file, returning a component state dict.
        
        Returns None on error.
        """
        try:
            with open(filePath, 'r') as f:
                return json.load(f)
        except Exception:
            self._logger.error(f'Could not open or parse scan file {filePath!r}:\n{traceback.format_exc()}')
            return None

    def getNextAxial(self):
        return None

    @staticmethod
    def _countRisingEdges(ttl):
        count = 0
        previous = 0
        for value in ttl:
            current = 1 if value else 0
            if current == 1 and previous == 0:
                count += 1
            previous = current
        return count

    def getNumCamTTL(self):
        numCamTTL = {}
        ttlSignals = self._master.scanManager.getTTLCycleSignalsDict(
            self._digitalParameterDict
        )
        for detector in self._setupInfo.detectors.keys():
            ttl = ttlSignals.get(detector, None)
            if ttl is not None:
                numCamTTL[detector] = self._countRisingEdges(ttl)
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
        # An abort can arrive while an iteration is still running.  Remember
        # it at run scope so the eventual NI-DAQ completion cannot re-arm a
        # repeat/continuous/sequence continuation.
        self._scanStopRequested = True
        self._repeatPending = False  # Cancel any pending repeat re-arm
        self.doingNonFinalPartOfSequence = False  # So that sigScanEnded is emitted
        if not self.isRunning:
            self.scanFailed()

    def _beginScanRun(self, *, sigScanStartingEmitted):
        """Reserve one complete run, or reject a duplicate from this owner.

        ``sigScanStartingEmitted=True`` identifies an internal continuation
        (repeat, sequence or axial follow-up). A new user/API start while this
        controller already owns a run is a no-op; it must never fall through
        to ``arm()``, be refused, and then call ``scanFailed()`` on the
        original still-running scan.
        """
        if getattr(self, '_scanCompletionPublishing', False):
            self._logger.warning(
                'Ignoring a re-entrant scan start while the previous '
                'completion is still being published.'
            )
            self.isRunning = False
            return None

        localToken = getattr(self, '_scanRunToken', None)
        activeRun = self._scanCoordinator.runForOwner(self)
        activeIteration = self._scanCoordinator.tokenForOwner(self)
        if (
            localToken is not None
            and activeRun is localToken
            and (
                bool(getattr(self, '_scanRunFailed', False))
                or bool(getattr(self, '_scanStopRequested', False))
                or bool(getattr(activeRun, 'releaseRequested', False))
            )
        ):
            self._logger.warning(
                'Ignoring a scan continuation while the current run is '
                'failed, stopped, or still releasing.'
            )
            self.isRunning = activeIteration is not None
            return None
        if localToken is not None and activeRun is localToken:
            if activeIteration is not None or not sigScanStartingEmitted:
                self._logger.warning(
                    'Ignoring duplicate scan start from the active owner.'
                )
                # Callers historically mark isRunning before entering this
                # helper.  Restore the real iteration state so a duplicate
                # during a repeat/axial gap cannot suppress the legitimate
                # deferred continuation.
                self.isRunning = activeIteration is not None
                return None
        elif localToken is not None:
            # A released token retained by a stale UI path must not be reused.
            self._scanRunToken = None
            self._scanRunStartingPublished = False

        isNewRun = activeRun is None
        token = self._scanCoordinator.reserveRun(self)
        if getattr(self, '_externalScanRequestInProgress', False):
            self._externalScanRequestAccepted = True
            self._externalScanRequestRunToken = token
            completion = getattr(
                self, '_externalScanRequestCompletion', None
            )
            if completion is not None:
                completion.bind(token)
                pending = self.__dict__.setdefault(
                    '_pendingExternalScanRequestCompletions', []
                )
                if completion not in pending:
                    pending.append(completion)
        if isNewRun:
            self._scanStopRequested = False
            self._scanRunFailed = False
        self._scanRunToken = token
        # Announce the active source only after the global coordinator has
        # granted this exact run. A losing controller must never replace the
        # source of the scan that actually owns the hardware.
        self.isRunning = True
        if sigScanStartingEmitted:
            # An external workflow published the run-level start before asking
            # this controller to arm.
            self._scanRunStartingPublished = True
        elif not self._scanRunStartingPublished:
            # Mark first so a signal-slot exception is still paired by the
            # failure path.
            self._scanRunStartingPublished = True
            self.emitScanSignal(self._commChannel.sigScanStarting)
        return token

    def _scanRunReleaseProven(self, token) -> bool:
        if token is None:
            return True
        if bool(getattr(token, 'released', False)):
            return True
        try:
            return self._scanCoordinator.activeRunToken is not token
        except Exception:
            return False

    def _claimScanRunTerminal(self, expectedRunToken=None):
        """Atomically take the one local terminal-publication claim.

        The coordinator makes release idempotent, but it cannot decide which of
        two controller callbacks owns ``sigScanEnded`` and the exact request
        terminal. Clearing the local identity under this lock gives exactly one
        path that publication authority.
        """
        terminalLock = self.__dict__.setdefault(
            '_scanRunTerminalLock', threading.RLock()
        )
        with terminalLock:
            token = getattr(self, '_scanRunToken', None)
            startingPublished = bool(
                getattr(self, '_scanRunStartingPublished', False)
            )
            if expectedRunToken is not None and token is not expectedRunToken:
                if not SuperScanController._scanRunReleaseProven(
                    self, expectedRunToken
                ):
                    raise RuntimeError(
                        'The captured scan run is still active but the '
                        'controller no longer holds its exact local identity.'
                    )
                return None, False, False
            if token is None and not startingPublished:
                return None, False, False

            # Clear before emitting: a re-entrant failure/end callback must be a
            # no-op, and a stale callback must never release a later run.
            self._scanRunToken = None
            self._scanRunStartingPublished = False
            return token, startingPublished, True

    def _finishScanRun(self, onReleased=None, *, expectedRunToken=None):
        """Release one exact run, then publish its end and optional terminal.

        ``onReleased(error)`` runs after ``sigScanEnded`` and only after the
        coordinator's detector-finish barrier has actually released the run.
        ``error`` is the end-signal publication failure, if any. It is also
        called for an already-absent/stale lifecycle so callers cannot strand
        an exact request terminal. ``expectedRunToken`` binds a terminal path
        to the run it captured before emitting compatibility signals, so
        re-entrant code can never make it release a newer run.
        """
        def deliverOnControllerThread(callback):
            invoke = getattr(
                self, '_invokeOnControllerThreadIfNeeded', None
            )
            if callable(invoke):
                try:
                    invoke(callback)
                    return
                except Exception:
                    # A deleted QObject or failed queued-signal handoff must
                    # not leave a holdUntilFinalized reservation permanently
                    # active. The publication callback is idempotent, so a
                    # handoff that queued and then raised is safe to replay.
                    self._logger.error(
                        'Failed to hand off scan terminal publication to the '
                        'controller thread; publishing through the fail-safe '
                        'path',
                        exc_info=True,
                    )
            callback()

        token, startingPublished, terminalClaimed = (
            SuperScanController._claimScanRunTerminal(
                self, expectedRunToken
            )
        )
        if not terminalClaimed:
            if onReleased is not None:
                deliverOnControllerThread(onReleased)
            return False
        callbackLock = threading.Lock()
        callbackDelivered = False
        finalizeRunRelease = getattr(
            self._scanCoordinator, 'finalizeRunRelease', None
        )
        holdUntilFinalized = bool(
            callable(finalizeRunRelease)
            and (startingPublished or onReleased is not None)
        )

        def publishEnded():
            nonlocal callbackDelivered
            with callbackLock:
                if callbackDelivered:
                    return
                callbackDelivered = True
            # Park before the terminal, not after: sigScanEnded is what hands
            # the focus axis back to the focus lock, which must not start
            # correcting against an actuator the scan left mid-waveform. A
            # no-op when the success path already did it for this run.
            restorePositioners = getattr(
                self, '_restoreScanPositionersIfPending', None
            )
            if callable(restorePositioners):
                restorePositioners()
            endError = None
            try:
                if startingPublished:
                    self.emitScanSignal(self._commChannel.sigScanEnded)
            except Exception as error:
                endError = error
                self._logger.error(
                    'Failed to publish the scan-run end signal',
                    exc_info=True,
                )
            finalizeError = None
            if holdUntilFinalized:
                try:
                    finalized = finalizeRunRelease(token)
                    if (
                        not finalized
                        and not SuperScanController._scanRunReleaseProven(
                            self, token
                        )
                    ):
                        raise RuntimeError(
                            'Scan coordinator did not finalize the held '
                            'run reservation after terminal publication.'
                        )
                except Exception as error:
                    finalizeError = error
                    self._logger.error(
                        'Failed to finalize the published scan-run release',
                        exc_info=True,
                    )
                    # The run-level end has already been attempted, so retain
                    # only the exact token identity. A later abort/shutdown can
                    # retry finalization without publishing a duplicate end.
                    if not SuperScanController._scanRunReleaseProven(
                        self, token
                    ):
                        if getattr(self, '_scanRunToken', None) is None:
                            self._scanRunToken = token
            if onReleased is not None:
                onReleased(endError or finalizeError)

        def publishEndedFromReleaseThread():
            deliverOnControllerThread(publishEnded)

        if token is not None:
            releaseAccepted = False
            try:
                releaseKwargs = {
                    'onReleased': (
                        publishEndedFromReleaseThread
                        if startingPublished or onReleased is not None
                        else None
                    ),
                }
                if holdUntilFinalized:
                    releaseKwargs['holdUntilFinalized'] = True
                releaseAccepted = self._scanCoordinator.releaseRun(
                    token, **releaseKwargs
                )
            except Exception:
                self._logger.error(
                    'Failed to release scan-run reservation',
                    exc_info=True,
                )
                releaseProven = SuperScanController._scanRunReleaseProven(
                    self, token
                )
                if not releaseProven:
                    # Restore the exact local lifecycle so a later failure,
                    # abort, or close can retry this same reservation. Never
                    # synthesize sigScanEnded or resolve an exact request while
                    # the coordinator may still own the token.
                    if getattr(self, '_scanRunToken', None) is None:
                        self._scanRunToken = token
                    if startingPublished:
                        self._scanRunStartingPublished = True
                    raise
                if not callbackDelivered:
                    publishEndedFromReleaseThread()
                return True
            # A stale/already-released reservation still needs its published
            # start paired.  A live deferred release accepted the callback and
            # will publish only after the participant barrier clears.
            if (
                (startingPublished or onReleased is not None)
                and not releaseAccepted
            ):
                releaseProven = SuperScanController._scanRunReleaseProven(
                    self, token
                )
                if not releaseProven:
                    if getattr(self, '_scanRunToken', None) is None:
                        self._scanRunToken = token
                    if startingPublished:
                        self._scanRunStartingPublished = True
                    raise RuntimeError(
                        'Scan coordinator did not accept or prove release of '
                        'the active run reservation.'
                    )
                publishEndedFromReleaseThread()
        else:
            publishEndedFromReleaseThread()
        return True

    def scanFailed(
        self, *, _terminalRunToken=None, _terminalCompletions=None,
        _forceFinalizeDuringPublication=False,
    ):
        """ Called when scan failed. """
        publicationAlreadyInProgress = bool(
            getattr(self, '_scanCompletionPublishing', False)
        )
        self._scanRunFailed = True
        if getattr(self, '_externalScanRequestInProgress', False):
            self._externalScanRequestFailed = True
            if not self._externalScanRequestFailureMessage:
                self._externalScanRequestFailureMessage = (
                    'Scan controller failed before the requested run armed.'
                )
        self._logger.error('Scan failed')
        self._repeatPending = False  # Cancel any pending repeat re-arm
        self.isRunning = False
        self.doingNonFinalPartOfSequence = False
        runToken = (
            _terminalRunToken
            if _terminalRunToken is not None else
            getattr(self, '_scanRunToken', None)
        )
        try:
            self._widget.setScanButtonChecked(False)
        except Exception:
            # Cosmetic cleanup must never bypass the exact hardware terminal.
            self._logger.error(
                'Failed to reset the scan widget after scan failure',
                exc_info=True,
            )
        if (
            publicationAlreadyInProgress
            and not _forceFinalizeDuringPublication
        ):
            # The outer success publisher owns the detached exact terminal and
            # release callback. Marking failure is sufficient; clearing its
            # gate or starting a second finish path would let a later listener
            # re-arm hardware before the outer signal emission returns.
            return
        completions = (
            tuple(_terminalCompletions)
            if _terminalCompletions is not None else
            self._detachExternalScanRequestCompletions(runToken)
        )
        self._scanCompletionPublishing = True

        def completeFailure(finishError=None):
            if (
                finishError is not None
                and not SuperScanController._scanRunReleaseProven(
                    self, runToken
                )
            ):
                # Finalization is still unproven. Preserve retry authority and
                # the exact request instead of reporting a terminal while the
                # global reservation remains held.
                self._requeueExternalScanRequestCompletions(
                    runToken, completions
                )
                self._scanCompletionPublishing = False
                return
            self._scanCompletionPublishing = False
            self._completeExternalScanRequest(
                runToken,
                successful=False,
                message=(
                    getattr(
                        self,
                        '_externalScanRequestFailureMessage',
                        '',
                    )
                    or 'Scan failed before successful completion.'
                ),
                completions=completions,
            )

        try:
            self._finishScanRun(
                onReleased=completeFailure,
                expectedRunToken=runToken,
            )
        except Exception:
            self._logger.error(
                'Failed to finalize the failed scan-run lifecycle',
                exc_info=True,
            )
            self._requeueExternalScanRequestCompletions(
                runToken, completions
            )
            self._scanCompletionPublishing = False

    def _armScanIteration(self, signalDict, scanInfoDict):
        """Publish the participating device list, then arm the iteration.

        Arming an iteration means this one has not parked its positioners yet.
        Clearing the flag here (rather than per run) keeps multi-part and
        repeat sequences parking per part exactly as they always have, while
        still letting the run terminal park a run that never reached its
        completion path at all.

        The publication has to happen HERE, before ``arm``. ``runScan`` marks
        the NI-DAQ manager busy and only then emits ``sigScanBuilt``, so any
        consumer that must issue a one-shot DAQ write in response to scan
        membership — laser arming above all — is already too late by the time
        ``sigScanBuilt`` arrives and its write is refused. Publishing first
        gives those consumers a window while the DAQ is still free.

        A failure to resolve the device list must not block the scan: the scan
        itself does not depend on this, only the consumers do.
        """
        self._scanPositionersRestored = False
        # Published here rather than alongside sigScanStarting, which fires
        # from _beginScanRun *before* getParameters has run: the parameter
        # dicts still describe the previous scan at that point, so a run that
        # newly added a Z axis could have been announced as not touching it.
        # Consumers have already yielded on sigScanStarting and this only ever
        # releases them, so arriving later is safe -- arriving wrong is not.
        self._publishScanActuators()
        try:
            devices = self._master.nidaqManager.resolveScanTTLDevices(signalDict)
        except Exception:
            self._logger.error(
                'Could not resolve the scan device list; lasers and other '
                f'membership consumers are not being notified:\n'
                f'{traceback.format_exc()}'
            )
        else:
            self.emitScanSignal(
                self._commChannel.sigScanDevicesResolved, devices
            )
        recordingManager = getattr(self._master, 'recordingManager', None)
        markScanStarted = getattr(
            recordingManager, 'markScanStarted', None
        )
        if callable(markScanStarted):
            try:
                markScanStarted(scanInfoDict)
            except Exception:
                # Recording liveness diagnostics must never prevent the scan
                # itself from arming.
                self._logger.error(
                    'Could not initialize the scan-recording watchdog',
                    exc_info=True,
                )
        return self._scanCoordinator.arm(signalDict, scanInfoDict, owner=self)

    def __onNidaqScanDone(self):
        """NI-DAQ finished a scan iteration.

        For the controller that armed it, ``scanDone`` — which ends the scan
        for the UI and arms the next repeat frame — is held back until every
        participant has acknowledged its end-of-scan work. Releasing the lease
        and re-arming while a detector is still reading out its final frame
        tears the worker down mid-read and loses that frame.

        Other scan controllers see the same NI-DAQ signal but do nothing: they
        do not own this iteration and must not publish an early sigScanEnded.
        """
        token = self._scanCoordinator.tokenForOwner(self)
        if token is None:
            return
        runToken = self._scanCoordinator.runForOwner(self)
        finishMode = (
            FINISH_ABORT
            if getattr(self, '_scanStopRequested', False)
            else FINISH_GRACEFUL
        )
        self._scanCoordinator.resolve(
            token,
            finishMode,
            onComplete=lambda: self.__afterScanFinishBarrier(runToken),
        )

    def __onNidaqScanBuildFailed(self):
        token = self._scanCoordinator.tokenForOwner(self)
        if token is None:
            return
        runToken = self._scanCoordinator.runForOwner(self)
        self._scanCoordinator.resolve(
            token,
            FINISH_ABORT,
            onComplete=lambda: self.__afterScanBuildFailureBarrier(runToken),
        )

    def __afterScanFinishBarrier(self, runToken):
        # The last acknowledgement may arrive on a detector's worker thread;
        # queue through this QObject's affinity before touching the widget or
        # re-arming. A static singleShot created on a plain worker can be lost.
        self._invokeOnControllerThread(
            lambda: self.__deliverScanDone(runToken)
        )

    def __afterScanBuildFailureBarrier(self, runToken):
        self._invokeOnControllerThread(
            lambda: self.__deliverScanFailed(runToken)
        )

    def __deliverScanDone(self, runToken):
        if (
            runToken is None
            or getattr(self, '_scanRunToken', None) is not runToken
            or self._scanCoordinator.runForOwner(self) is not runToken
        ):
            return
        self.scanDone()

    def __deliverScanFailed(self, runToken):
        if (
            runToken is None
            or getattr(self, '_scanRunToken', None) is not runToken
            or self._scanCoordinator.runForOwner(self) is not runToken
        ):
            return
        self.scanFailed()

    def _armRepeatScan(self):
        """Schedule the next repeat-scan frame on the next event-loop turn.

        ``scanDone`` runs inside the NidaqManager ``sigScanDone`` handler, which
        itself fires from the just-finished scan's task-completion slot. Calling
        ``runScanAdvanced`` (and therefore ``nidaqManager.runScan``) directly
        from there re-enters the scan machinery while the previous scan's NI-DAQ
        tasks, WaitThreads and per-detector scan QThreads are still tearing down.
        On real hardware that recreates/reassigns those objects while the old
        ones are mid-shutdown — a QThread destroyed while still running — which
        crashes the GUI when 'Repeat' is enabled.

        Deferring the re-arm with a zero-delay timer lets the current signal
        chain unwind and the previous scan fully release its resources before
        the next frame arms. ``_repeatPending`` is cleared by ``abortScan`` /
        ``scanFailed`` so a stop/abort in the gap cancels the pending frame.
        """
        self._repeatPending = True
        QtCore.QTimer.singleShot(0, self._fireRepeatScan)

    def _fireRepeatScan(self):
        """Deferred continuation of a repeat scan (see _armRepeatScan)."""
        if not self._repeatPending:
            return  # Aborted or superseded while the re-arm was pending
        self._repeatPending = False
        if self.isRunning:
            return  # A scan is already running; don't stack another
        try:
            shouldContinue = self._shouldContinueRepeat()
        except Exception:
            self._logger.error(
                'Failed to determine whether the repeat scan should continue',
                exc_info=True,
            )
            self.scanFailed()
            return
        if not shouldContinue:
            # ``scanDone`` retained the run reservation because repeat was
            # enabled at that instant. If the user disables it before this
            # deferred callback, terminalize exactly as scanDone would have.
            isFinalPart = not self.doingNonFinalPartOfSequence
            if isFinalPart:
                try:
                    self._widget.setScanButtonChecked(False)
                except Exception:
                    self._logger.error(
                        'Failed to reset the scan widget after completion',
                        exc_info=True,
                    )
            self._publishScanDone(isFinalPart=isFinalPart)
            return
        self.runScanAdvanced(sigScanStartingEmitted=True)

    def _shouldContinueRepeat(self) -> bool:
        """Whether a deferred repeat frame should still fire when it comes due.

        Default: only while the widget's 'Repeat' box is checked. Controllers
        that also loop in continuous-laser mode override this to keep looping
        there too.
        """
        if getattr(self, '_scanStopRequested', False):
            return False
        repeatEnabled = getattr(self._widget, 'repeatEnabled', None)
        return bool(repeatEnabled()) if callable(repeatEnabled) else False

    def closeEvent(self) -> bool:
        """Cancel this run and report whether its NI/detector barrier drained."""
        self._scanClosing = True
        self.abortScan()
        super().closeEvent()
        return self.shutdownComplete()

    def shutdownComplete(self) -> bool:
        """Whether this controller owns no iteration or run-level reservation."""
        coordinator = getattr(self, '_scanCoordinator', None)
        pendingCompletion = False
        for completion in tuple(getattr(
            self, '_pendingExternalScanRequestCompletions', ()
        ) or ()):
            wait = getattr(completion, 'wait', None)
            try:
                if not callable(wait) or not wait(timeout=0):
                    pendingCompletion = True
                    break
            except Exception:
                pendingCompletion = True
                break
        coordinatorComplete = True
        if coordinator is not None:
            try:
                coordinatorComplete = (
                    coordinator.tokenForOwner(self) is None
                    and coordinator.runForOwner(self) is None
                )
            except Exception:
                # Teardown must fail closed if exact coordinator ownership
                # cannot be inspected. Reporting success here would allow the
                # application to destroy a controller whose hardware terminal
                # may still be pending.
                coordinatorComplete = False
                logger = getattr(self, '_logger', None)
                if logger is not None:
                    try:
                        logger.error(
                            'Unable to verify scan ownership during shutdown',
                            exc_info=True,
                        )
                    except Exception:
                        pass
        return (
            coordinatorComplete
            and not getattr(self, '_repeatPending', False)
            and not getattr(self, 'isRunning', False)
            and not getattr(self, '_scanCompletionPublishing', False)
            and not getattr(self, '_scanRunStartingPublished', False)
            and not pendingCompletion
        )

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

    def _getPositionerManagerProperties(self, positionerName):
        positionerInfo = self._setupInfo.positioners.get(positionerName)
        if positionerInfo is None:
            return {}
        return getattr(positionerInfo, 'managerProperties', {}) or {}

    def _getReturnToCenterAxis(self, positionerName):
        properties = self._getPositionerManagerProperties(positionerName)
        return properties.get('returnToCenterAfterScanAxis', 0)

    def _positionerReturnsToCenterAfterScan(self, positionerName):
        properties = self._getPositionerManagerProperties(positionerName)
        return bool(properties.get('returnToCenterAfterScan', False))

    def _resetReturnToCenterPositionersAfterScan(self):
        targetDevices = self._analogParameterDict.get('target_device', [])
        centerPositions = self._analogParameterDict.get('axis_centerpos', [])

        for index, positionerName in enumerate(targetDevices):
            if positionerName == 'None':
                continue
            if not self._positionerReturnsToCenterAfterScan(positionerName):
                continue
            if index >= len(centerPositions):
                self._logger.warning(
                    'Cannot reset %s after scan because no center position is configured.',
                    positionerName,
                )
                continue

            position = centerPositions[index]
            axis = self._getReturnToCenterAxis(positionerName)
            self._master.positionersManager[positionerName].setPosition(position, axis)

    def _restoreScanPositioners(self):
        """Park marked positioners at their centre after an iteration.

        Two orderings have to hold at once. The parked stage must be real
        before ``sigScanDone``, which per-iteration consumers and the UI react
        to; and before ``sigScanEnded``, which is what hands the focus axis
        back to the focus lock -- resuming a lock against an actuator still
        sitting wherever the waveform ended is the whole problem this exists
        to avoid.

        A stage that refuses to park is a warning, never a reason to strand
        the run without a terminal.
        """
        self._scanPositionersRestored = True
        try:
            self._resetReturnToCenterPositionersAfterScan()
        except Exception:
            self._logger.warning(
                "Failed to reset positioners after scan:\n%s",
                traceback.format_exc(),
            )

    def _restoreScanPositionersIfPending(self):
        """Park at the run terminal, unless the finished iteration already did.

        Only the completion path used to park, and only in two of the scan
        controllers, so a failed or aborted run released the actuator wherever
        the waveform left it -- and then released the focus lock onto it. The
        flag is cleared as each iteration arms, so a normal multi-part or
        repeat sequence still parks per part exactly as before and this adds
        no second hardware write.
        """
        if self.__dict__.get('_scanPositionersRestored', False):
            return
        self._restoreScanPositioners()

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
