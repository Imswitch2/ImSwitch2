import os
import inspect
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Union, List, Dict, Any
import numpy as np
from qtpy import QtCore

from imswitch.imcommon.framework import Signal, Timer
from imswitch.imcommon.model import ostools, APIExport
from imswitch.imcontrol.model import RecMode, SaveMode, SaveFormat, getWidgetStatePersistence
from imswitch.imcontrol.model.managers.RecordingManager import (
    RECORDING_ARM_TIMEOUT, FailureKind,
)
from ..basecontrollers import ImConWidgetController, StatefulComponentMixin, ComponentStateApplyMode
from imswitch.imcommon.model import initLogger

# Poll interval used to wait out a still-finalizing recording before starting
# the next timelapse timepoint (see nextLapse). Small so a Freq=0 lapse advances
# as soon as the previous recording clears, without busy-spinning the GUI thread.
_LAPSE_RECORDING_DRAIN_RETRY_MS = 25
# QTimer uses a signed 32-bit millisecond interval on supported Qt versions.
# Long camera lapses are scheduled in bounded chunks so intervals longer than
# roughly 24 days cannot wrap into an immediate timer.
_MAX_LAPSE_TIMER_MS = 2_147_000_000


def _dispatchWithoutLifecycle(dispatch, *args):
    """Dispatch a scan without letting the dispatcher publish its start.

    A dispatcher predating ``notify_starting`` never published one, which
    is exactly what is being asked for -- so falling back to the positional
    call is the requested behaviour, not a degraded approximation of it.
    The fallback is narrowed to this call's own signature mismatch: an
    adapter that accepts the keyword and then raises ``TypeError`` from
    inside must not be silently re-dispatched, which would start the scan
    twice.
    """
    try:
        return dispatch(*args, notify_starting=False)
    except TypeError as error:
        if 'notify_starting' not in str(error):
            raise
    return dispatch(*args)


class RecordingController(ImConWidgetController, StatefulComponentMixin):
    """ Linked to RecordingWidget. """

    _sigExactScanRequestCompleted = Signal(object)

    componentName = 'Recording'
    stateSchemaVersion = 1
    legacyStateNames = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self)
        self._widget.setDetectorList(
            self._master.detectorsManager.execOnAll(lambda c: c.model,
                                                    condition=lambda c: c.forAcquisition)
        )
        self._widget.setLasersList(
            self._master.lasersManager.execOnAll(lambda c: c.name)
        )

        self.settingAttr = False
        self.recording = False
        self.doneScan = False
        self.endedRecording = False
        # Guards the programmatic REC-button reset in recordingCycleEnded() from
        # re-entering toggleREC() as if the user had pressed stop (which would
        # end a scan recording still draining toward its frame target).
        self._finalizingRecCycle = False
        self.lapseCurrent = -1
        self.specLapseCurrent = -1
        self.lapseTotal = 0
        self.stopRequested = False
        self.timer = None
        self._cameraLapseIntervalS = None
        self._cameraLapseNextDeadline = None
        self._cameraLapsePlannedStart = None
        self._scanStartPublished = False
        self._scanRequestAccepted = False
        self._acceptedScanRunToken = None
        self._acceptedScanCompletion = None
        self._exactScanCompletionHandled = False
        self._scanLifecycleEndedObserved = False
        self._recordingScanSource = None
        self._recordingOperationActive = False
        self._recordingManagerGeneration = None
        self._recordingGenerationBeforeOperation = None
        self._recordingFailureHandled = False
        self._recordingFailureAwaitingScanEnd = False
        self._recordingFailedCurrent = False
        self._recordingCycleTerminalHandled = False
        self._shutdownRequested = False
        # Set while a scan-once recording is armed and waiting for the operator
        # to start a scan from one of several scan widgets. The frame
        # expectation is read from whichever controller announces itself, not
        # guessed at arm time. See _activeScanSourceChanged.
        self._awaitingScanSourceArm = False
        # Widget key of the timelapse-scan source, surviving repopulation of
        # the chooser and restored from the saved component state.
        self._scanSourcePreference = ''
        # Set when a late-bound scan owns the run-level lifecycle instead of
        # this controller. _scanStartPublished stays False in that case, so the
        # terminal checks need this to tell "no scan was ever expected" from
        # "a scan was expected and somebody else published its start".
        self._scanStartOwnedByScanSource = False

        self._widget.setsaveFormat(SaveFormat.HDF5.value)
        self._widget.setSnapSaveMode(SaveMode.Disk.value)
        self._widget.setSnapSaveModeVisible(self._setupInfo.hasWidget('Image'))

        self._widget.setRecSaveMode(SaveMode.Disk.value)
        self._widget.setRecSaveModeVisible(
            self._moduleCommChannel.isModuleRegistered('improcess')
        )

        self.untilStop()

        # Connect CommunicationChannel signals
        recordingManager = self._master.recordingManager
        detailedRecordingSignals = all(
            hasattr(getattr(recordingManager, name, None), 'connect')
            for name in (
                'sigRecordingStartedDetailed',
                'sigRecordingEndedDetailed',
                'sigRecordingFailedDetailed',
            )
        )
        self._usesDetailedRecordingSignals = detailedRecordingSignals
        if detailedRecordingSignals:
            recordingManager.sigRecordingStartedDetailed.connect(
                self._recordingStartedDetailed
            )
            recordingManager.sigRecordingEndedDetailed.connect(
                self._recordingEndedDetailed
            )
            recordingManager.sigRecordingFailedDetailed.connect(
                self._recordingFailedDetailed
            )
        else:
            self._commChannel.sigRecordingStarted.connect(
                self.recordingStarted
            )
            self._commChannel.sigRecordingEnded.connect(self.recordingEnded)
            self._commChannel.sigRecordingFailed.connect(
                self.recordingFailed
            )
        self._commChannel.sigScanDone.connect(self.scanDone)
        self._commChannel.sigScanEnded.connect(self._scanLifecycleEnded)
        self._commChannel.sigUpdateRecFrameNum.connect(self.updateRecFrameNum)
        self._commChannel.sigUpdateRecTime.connect(self.updateRecTime)
        self._commChannel.sharedAttrs.sigAttributeSet.connect(self.attrChanged)
        self._commChannel.sigSnapImg.connect(self.snap)
        self._commChannel.sigSnapImgPrev.connect(self.snapImagePrev)
        self._commChannel.sigStartRecordingExternal.connect(self.startRecording)
        self._commChannel.sigStartRecording.connect(self.startRecording)
        self._commChannel.sigStopRecording.connect(self.stopRecording)
        self._commChannel.sigRequestScanFreq.connect(self.sendScanFreq)
        queuedConnection = getattr(
            QtCore.Qt, 'ConnectionType', QtCore.Qt
        ).QueuedConnection
        try:
            self._sigExactScanRequestCompleted.connect(
                self._exactScanRequestCompleted,
                type=queuedConnection,
            )
        except TypeError:
            self._sigExactScanRequestCompleted.connect(
                self._exactScanRequestCompleted,
                queuedConnection,
            )

        # Connect RecordingWidget signals
        self._widget.sigDetectorModeChanged.connect(self.detectorChanged)
        self._widget.sigDetectorSpecificChanged.connect(self.detectorChanged)
        self._widget.sigOpenRecFolderClicked.connect(self.openFolder)
        self._widget.sigSpecFileToggled.connect(self._widget.setCustomFilenameEnabled)

        self._widget.sigSnapSaveModeChanged.connect(self.snapSaveModeChanged)

        self._widget.sigSpecFramesPicked.connect(self.specFrames)
        self._widget.sigSpecTimePicked.connect(self.specTime)
        self._widget.sigSpecLapsePicked.connect(self.specLapse)
        self._widget.sigScanOncePicked.connect(self.recScanOnce)
        self._widget.sigScanLapsePicked.connect(self.recScanLapse)
        self._widget.sigUntilStopPicked.connect(self.untilStop)

        self._widget.sigSnapRequested.connect(self.snap)
        self._widget.sigRecToggled.connect(self.toggleREC)
        
        # Register for unified state persistence (canonical name)
        getWidgetStatePersistence().register('Recording', self)

    def openFolder(self):
        """ Opens current folder in File Explorer. """
        folder = self._widget.getRecFolder()
        if not os.path.exists(folder):
            os.makedirs(folder)
        ostools.openFolderInOS(folder)

    def snapSaveModeChanged(self):
        saveMode = SaveMode(self._widget.getSnapSaveMode())
        self._widget.setsaveFormatEnabled(saveMode != SaveMode.RAM)
        if saveMode == SaveMode.RAM:
            self._widget.setsaveFormat(SaveFormat.TIFF.value)

    def snap(self):
        """ Take a snap and save it to a file. """
        if self.__dict__.get('_shutdownRequested', False):
            return False
        self.updateRecAttrs(isSnapping=True)

        folder = self._widget.getRecFolder()
        if not os.path.exists(folder):
            os.makedirs(folder)
        time.sleep(0.01)

        detectorNames = self.getDetectorNamesToCapture()
        savename = os.path.join(folder, self.getFileName()) + '_snap'

        attrs = {detectorName: self._commChannel.sharedAttrs.getHDF5Attributes()
                 for detectorName in detectorNames}
        
        self._master.recordingManager.snap(detectorNames,
                                           savename,
                                           SaveMode(self._widget.getSnapSaveMode()),
                                           SaveFormat(self._widget.getSaveSnapFormat()),
                                           attrs)
        return True
        
    def snapNumpy(self):
        if self.__dict__.get('_shutdownRequested', False):
            return False
        self.updateRecAttrs(isSnapping=True)
        detectorNames = self.getDetectorNamesToCapture()
        attrs = {detectorName: self._commChannel.sharedAttrs.getHDF5Attributes()
                 for detectorName in detectorNames}

        return self._master.recordingManager.snap(detectorNames,
                                           "",
                                           SaveMode(4), # for Numpy
                                           "",
                                           attrs)

    def snapImagePrev(self, *args):
        """ Snap an already taken image and save it to a file. """
        self.updateRecAttrs(isSnapping=True)

        args = list(args)
        detectorName = (args[0])
        image = args[1]
        suffix = args[2]

        folder = self._widget.getRecFolder()
        if not os.path.exists(folder):
            os.makedirs(folder)
        time.sleep(0.01)

        savename = os.path.join(folder, self.getFileName()) + '_snap_' + suffix
        attrs = {detectorName: self._commChannel.sharedAttrs.getHDF5Attributes()}

        self._master.recordingManager.snapImagePrev(detectorName,
                                                    savename,
                                                    SaveFormat(self._widget.getSaveSnapFormat()),
                                                    image,
                                                    attrs)

    def toggleREC(self, checked):
        """ Start or end recording. """
        if self.__dict__.get('_shutdownRequested', False):
            return
        if self._finalizingRecCycle:
            # Programmatic REC-button reset during scan/lapse cycle finalization
            # (recordingCycleEnded), not a user stop. Re-entering here would call
            # endRecording() and truncate a scan recording that is still draining
            # toward its frame target (the worker ends itself via should_stop).
            return
        if checked and not self.recording:
            self.stopRequested = False
            # Open a fresh two-terminal window before the writer can arm. A
            # completed previous ScanOnce must not let an early writer terminal
            # satisfy the new scan, and a synchronous writer terminal must not
            # be overwritten later in this method.
            self.doneScan = False
            self.endedRecording = False
            self._scanRequestAccepted = False
            self._acceptedScanRunToken = None
            self._acceptedScanCompletion = None
            self._exactScanCompletionHandled = False
            self._scanLifecycleEndedObserved = False
            self._recordingScanSource = None
            self._awaitingScanSourceArm = False
            self._scanStartOwnedByScanSource = False
            self._recordingOperationActive = True
            self._recordingManagerGeneration = None
            self._recordingFailureHandled = False
            self._recordingFailureAwaitingScanEnd = False
            self._recordingFailedCurrent = False
            self._recordingCycleTerminalHandled = False
            try:
                managerGeneration = getattr(
                    self._master.recordingManager,
                    'recordingGeneration',
                    None,
                )
                self._recordingGenerationBeforeOperation = (
                    managerGeneration
                    if isinstance(managerGeneration, int) else None
                )
                self.updateRecAttrs(isSnapping=False)

                folder = self._widget.getRecFolder()
                if not os.path.exists(folder):
                    os.makedirs(folder)
                time.sleep(0.01)
                self.savename = (
                    os.path.join(folder, self.getFileName()) + '_rec'
                )

                if self.recMode == RecMode.ScanOnce:
                    if not self._preflightNewScanRequest():
                        return
                    if self._commChannel.hasScanWidget():
                        self._recordingScanSource = (
                            self._commChannel.getRecordingScanSource()
                        )
                        self._notifyScanStarting()
                    else:
                        # Standalone setups arm here and the operator starts
                        # the scan from its own widget, so the scanner is not
                        # known yet. Reading the geometry now would freeze
                        # whichever controller happens to expose the accessors
                        # — the wrong one as soon as a rig has several. Defer
                        # to _armScanOnceFromActiveSource, and leave both
                        # lifecycle boundaries with the controller that runs.
                        self._awaitingScanSourceArm = True

                detectorsBeingCaptured = self.getDetectorNamesToCapture()

                self.recordingArgs = {
                    'detectorNames': detectorsBeingCaptured,
                    'recMode': self.recMode,
                    'savename': self.savename,
                    'saveMode': SaveMode(self._widget.getRecSaveMode()),
                    'saveFormat': SaveFormat(self._widget.getSaveFormat()),
                    'attrs': {
                        detectorName:
                            self._commChannel.sharedAttrs.getHDF5Attributes()
                        for detectorName in detectorsBeingCaptured
                    },
                    'singleMultiDetectorFile': (
                        len(detectorsBeingCaptured) > 1
                        and self._widget.getMultiDetectorSingleFile()
                    ),
                }
            except Exception as error:
                self._handleRecordingFailure(
                    str(error),
                    abortManager=bool(
                        getattr(
                            self._master.recordingManager,
                            'record',
                            False,
                        )
                    ),
                )
                return

            if self.recMode == RecMode.SpecFrames:
                self.recordingArgs['recFrames'] = self._widget.getNumExpositions()
                if not self._startManagerRecording():
                    return
            elif self.recMode == RecMode.SpecTime:
                self.recordingArgs['recTime'] = self._widget.getTimeToRec()
                if not self._startManagerRecording():
                    return
            elif self.recMode == RecMode.CameraLapse:
                try:
                    self.lapseTotal = (
                        self._widget.getTimelapseNumFrames()
                    )
                    self._cameraLapseIntervalS = (
                        self._widget.getSpecTimelapseFrameTime()
                    )
                    self._validateCameraLapse(
                        detectorsBeingCaptured,
                        self.lapseTotal,
                        self._cameraLapseIntervalS,
                        SaveFormat(self._widget.getSaveFormat()),
                        self._widget.getTimelapseSingleFile(),
                    )
                    self.recordingArgs['singleLapseFile'] = (
                        self._widget.getTimelapseSingleFile()
                    )
                    self.recordingArgs['recFrames'] = 1
                    self.lapseCurrent = 0
                    self._cameraLapseNextDeadline = time.monotonic()
                    self._cameraLapsePlannedStart = datetime.now(
                        timezone.utc
                    )
                    # Camera lapse is one logical UI operation containing
                    # several short manager sessions.
                    self.recording = True
                    if not self.nextCameraLapse():
                        return
                    return
                except Exception as error:
                    self._handleRecordingFailure(
                        str(error),
                        abortManager=bool(
                            getattr(
                                self._master.recordingManager,
                                'record',
                                False,
                            )
                        ),
                    )
                    return
            elif self.recMode == RecMode.ScanOnce:
                if self._awaitingScanSourceArm:
                    # Setups with standalone scan widgets (e.g. TriggerScope)
                    # register SEVERAL controllers on sigRunScan; broadcasting
                    # run_scan would start all of their scans at once. Arm the
                    # recording only and let the user start the intended scan
                    # from its own widget. The manager is armed once that scan
                    # announces itself, so its geometry is the one recorded.
                    self.__logger.info(
                        'Recording armed (scan-once): start the scan from its '
                        'scan widget to begin acquiring frames.'
                    )
                else:
                    if not self._applyScanGeometryToRecordingArgs():
                        return
                    if not self._startManagerRecording():
                        return
                    if not self._waitForManagerArm():
                        return
                    if not self._requestScanStart(True, False):
                        return
            elif self.recMode == RecMode.ScanLapse:
                singleFile = self._widget.getTimelapseSingleFile()
                if singleFile and SaveFormat(
                    self._widget.getSaveFormat()
                ) == SaveFormat.TIFF:
                    # The same rule the camera timelapse already enforces: a
                    # grouped lapse file has to be reopened per timepoint, and
                    # TIFF cannot be reopened safely.
                    self._handleRecordingFailure(
                        'Single-file scan timelapse supports HDF5 and ZARR; '
                        'select separate files for TIFF.',
                        abortManager=False, kind=FailureKind.WRITER,
                    )
                    return
                self.recordingArgs['singleLapseFile'] = singleFile
                self.lapseTotal = self._widget.getTimelapseTime()
                self.lapseCurrent = 0
                if not self.nextLapse():
                    return
            else:
                if not self._startManagerRecording():
                    return

            self.recording = True
        else:
            if (
                self.recMode == RecMode.CameraLapse
                and self.lapseCurrent != -1
            ):
                self.stopRequested = True
                timer = self.__dict__.get('timer')
                self.timer = None
                if timer is not None:
                    try:
                        timer.stop()
                    except Exception:
                        self.__logger.error(
                            'Failed to stop camera-lapse timer',
                            exc_info=True,
                        )
                managerActive = bool(
                    getattr(
                        self._master.recordingManager, 'record', False
                    )
                )
                if managerActive:
                    try:
                        self._master.recordingManager.abortRecording(
                            emitSignal=False,
                            wait=True,
                        )
                    except Exception as error:
                        self._recordingFailureHandled = False
                        self._handleRecordingFailure(
                            str(error), abortManager=False
                        )
                        return
                self.endedRecording = True
                self._recordingCycleTerminalHandled = False
                self.recordingCycleEnded()
                return
            if self.recMode == RecMode.ScanLapse and self.lapseCurrent != -1:
                # Soft-stop the current point but also terminalize the retained
                # run identity. Non-final lapse parts deliberately keep that
                # reservation between points; merely cancelling the cadence
                # timer would leak it and block every later NI scan.
                self.stopRequested = True

                timerWasActive = (
                    self.timer is not None and hasattr(self.timer, 'isActive') and self.timer.isActive()
                )
                if self.timer is not None:
                    self.timer.stop()
                    self.timer = None

                # The preceding non-final point closed its cycle guard when it
                # scheduled this cadence timer. Re-open it before aborting the
                # retained run: a targeted abort may synchronously emit the
                # run terminal, which must be allowed to finalize the stopped
                # lapse exactly once.
                self._recordingCycleTerminalHandled = False
                recordingManager = self._master.recordingManager
                managerWasActive = bool(
                    getattr(recordingManager, 'record', False)
                )
                if managerWasActive:
                    try:
                        # Stopping the scan without stopping its frame sink
                        # strands the writer at its original frame target.
                        # Abort both sides and classify their terminals as the
                        # expected result of this user stop.
                        recordingManager.abortRecording(
                            emitSignal=False,
                            wait=True,
                        )
                    except Exception as error:
                        self._handleRecordingFailure(
                            str(error), abortManager=False
                        )
                        return
                    self.endedRecording = True

                scanRunActive = (
                    self._scanRequestAccepted
                    and self._scanRunIsActive()
                )
                abortRetainedRun = (
                    self._scanRequestAccepted
                    and (scanRunActive or timerWasActive)
                )
                if abortRetainedRun:
                    try:
                        self._abortOwnedScanSequence()
                    except Exception:
                        self.__logger.error(
                            'Failed to stop the recording-owned scan lapse',
                            exc_info=True,
                        )
                if (
                    not scanRunActive
                    and (timerWasActive or managerWasActive)
                ):
                    self.doneScan = True
                    self._notifyScanEndedIfPending()
                    self.recordingCycleEnded()
                return
            if (
                self.recMode == RecMode.ScanOnce
                and not self.__dict__.get(
                    '_scanRequestAccepted', False
                )
            ):
                getActiveSource = getattr(
                    self._commChannel, 'getActiveScanSource', None
                )
                try:
                    activeSource = (
                        getActiveSource()
                        if callable(getActiveSource) else None
                    )
                except Exception:
                    activeSource = object()
                if activeSource is None:
                    # Manual standalone ScanOnce may be armed before the user
                    # starts any scan. Pair the pre-arm lifecycle explicitly;
                    # no future scan terminal exists to complete this cancel.
                    self.stopRequested = True
                    self.doneScan = True
                    self._notifyScanEndedIfPending()
                    if self._awaitingScanSourceArm:
                        # Nothing was ever handed to the manager, so its
                        # endRecording() produces no terminal to reset this
                        # operation. Without local cleanup the arm stays
                        # pending and the next scan the operator starts would
                        # begin recording with REC switched off.
                        self._cancelPendingScanSourceArm()
                        return
                    self._master.recordingManager.endRecording()
                    return
            self._master.recordingManager.endRecording()

    def _validateCameraLapse(
        self,
        detectorNames,
        totalFrames,
        intervalSeconds,
        saveFormat,
        singleLapseFile,
    ) -> None:
        if totalFrames < 1:
            raise ValueError(
                'Camera timelapse requires at least one frame.'
            )
        if intervalSeconds < 0:
            raise ValueError(
                'Camera timelapse interval cannot be negative.'
            )
        if singleLapseFile and saveFormat == SaveFormat.TIFF:
            raise ValueError(
                'Single-file timelapse supports HDF5 and ZARR; '
                'select separate files for TIFF.'
            )

        for detectorName in detectorNames:
            detector = self._master.detectorsManager[detectorName]
            if bool(getattr(detector, 'isScanDriven', False)):
                raise ValueError(
                    f'Camera timelapse cannot use scan-driven detector '
                    f'"{detectorName}".'
                )

            # Cameras exposing ImSwitch's standard trigger parameter can be
            # checked before opening a writer. Managers without that parameter
            # retain their existing free-running contract and are validated by
            # the normal no-progress watchdog.
            parameters = getattr(detector, 'parameters', {}) or {}
            triggerParameter = next(
                (
                    parameter
                    for name, parameter in parameters.items()
                    if str(name).strip().casefold() == 'trigger source'
                ),
                None,
            )
            if triggerParameter is None:
                continue
            triggerValue = getattr(
                triggerParameter, 'value', triggerParameter
            )
            if 'external' in str(triggerValue).casefold():
                raise ValueError(
                    f'Camera timelapse requires an internal/free-running '
                    f'trigger; detector "{detectorName}" is configured as '
                    f'{triggerValue!r}.'
                )

    def _assertCameraLapsePointIsIdle(self) -> None:
        getActiveSource = getattr(
            self._commChannel, 'getActiveScanSource', None
        )
        if callable(getActiveSource) and getActiveSource() is not None:
            raise RuntimeError(
                'Camera timelapse stopped because a scan was active at the '
                'scheduled timepoint.'
            )

        coordinator = getattr(
            self._master, 'scanExecutionCoordinator', None
        )
        if (
            coordinator is not None
            and getattr(coordinator, 'activeRunToken', None) is not None
        ):
            raise RuntimeError(
                'Camera timelapse stopped because a scan run was active at '
                'the scheduled timepoint.'
            )

        if bool(getattr(self._master.recordingManager, 'record', False)):
            raise RuntimeError(
                'Camera timelapse stopped because another recording was '
                'active at the scheduled timepoint.'
            )

    def nextCameraLapse(self):
        """Start one exact one-frame camera-lapse transaction."""
        if self.__dict__.get('_shutdownRequested', False):
            return False

        def failPoint(message, *, abortManager=False):
            self._recordingCycleTerminalHandled = False
            self._recordingFailureHandled = False
            self._handleRecordingFailure(
                message,
                abortManager=abortManager,
            )
            return False

        try:
            checked = bool(self._widget.isRecButtonChecked())
        except Exception as error:
            return failPoint(
                'Could not inspect camera-timelapse state before the next '
                f'timepoint: {error}'
            )
        if self.stopRequested or not checked:
            self._recordingCycleTerminalHandled = False
            self.recordingCycleEnded()
            return False

        recordingManager = self._master.recordingManager
        try:
            managerRecording = bool(recordingManager.record)
        except Exception as error:
            return failPoint(
                'Could not inspect recording-manager state before the next '
                f'camera timepoint: {error}'
            )
        if managerRecording:
            return failPoint(
                'Camera timelapse stopped because another recording was '
                'active at the scheduled timepoint.'
            )

        managerShutdownComplete = getattr(
            recordingManager, 'shutdownComplete', None
        )
        if callable(managerShutdownComplete):
            try:
                previousPointDrained = bool(managerShutdownComplete())
            except Exception as error:
                return failPoint(
                    'Could not inspect the previous camera-timelapse writer '
                    f'drain: {error}'
                )
            if not previousPointDrained:
                try:
                    timer = Timer(singleShot=True)
                    self.timer = timer
                    timer.timeout.connect(self.nextCameraLapse)
                    timer.start(_LAPSE_RECORDING_DRAIN_RETRY_MS)
                    return True
                except Exception as error:
                    timer = self.__dict__.get('timer')
                    if timer is not None:
                        try:
                            timer.stop()
                        except Exception:
                            pass
                    self.timer = None
                    return failPoint(
                        'Could not wait for the previous camera-timelapse '
                        f'writer to drain: {error}'
                    )

        try:
            self._assertCameraLapsePointIsIdle()
            self.endedRecording = False
            self.doneScan = True
            self._recordingCycleTerminalHandled = False

            if not self.recordingArgs['singleLapseFile']:
                width = max(1, len(str(self.lapseTotal)))
                index = str(self.lapseCurrent).zfill(width)
                self.recordingArgs['savename'] = (
                    f'{self.savename}_time{index}'
                )
            else:
                self.recordingArgs['savename'] = self.savename

            self.recordingArgs['attrs'] = {
                detectorName:
                    self._commChannel.sharedAttrs.getHDF5Attributes()
                for detectorName in self.recordingArgs['detectorNames']
            }
            self.recordingArgs['recFrames'] = 1
            self.recordingArgs['recLapseTotal'] = self.lapseTotal
            self.recordingArgs['recLapseIndex'] = self.lapseCurrent
            self.recordingArgs['recLapseIntervalS'] = (
                self._cameraLapseIntervalS
            )
            planned = self.__dict__.get('_cameraLapsePlannedStart')
            self.recordingArgs['recLapseScheduledTime'] = (
                planned.isoformat()
                if isinstance(planned, datetime) else None
            )
        except Exception as error:
            return failPoint(str(error))

        return self._startManagerRecording()

    def _scheduleCameraLapseTimer(self) -> bool:
        deadline = self.__dict__.get('_cameraLapseNextDeadline')
        if deadline is None:
            raise RuntimeError(
                'Camera timelapse has no next scheduled deadline.'
            )
        remainingSeconds = max(0.0, deadline - time.monotonic())
        delayMs = min(
            _MAX_LAPSE_TIMER_MS,
            max(0, int(round(remainingSeconds * 1000))),
        )
        timer = Timer(singleShot=True)
        self.timer = timer
        timer.timeout.connect(self._cameraLapseTimerFired)
        timer.start(delayMs)
        return True

    def _cameraLapseTimerFired(self):
        """Complete long timer chunks without burst-catching missed points."""
        if (
            self.__dict__.get('_shutdownRequested', False)
            or self.stopRequested
        ):
            return False
        deadline = self.__dict__.get('_cameraLapseNextDeadline')
        if deadline is None:
            return False
        if time.monotonic() + 0.001 < deadline:
            try:
                return self._scheduleCameraLapseTimer()
            except Exception as error:
                self._recordingCycleTerminalHandled = False
                self._recordingFailureHandled = False
                self._handleRecordingFailure(
                    f'Could not continue the camera-timelapse timer: {error}',
                    abortManager=False,
                )
                return False
        self.timer = None
        return self.nextCameraLapse()

    def nextLapse(self):
        if self.__dict__.get('_shutdownRequested', False):
            return False

        def failLapseEntry(message):
            # A cadence timer fires after recordingCycleEnded closed its
            # per-point guard while deliberately retaining the scan owner.
            # Re-open both guards so entry/retry failures can abort that exact
            # run and complete ordinary authoritative cleanup.
            self._recordingCycleTerminalHandled = False
            self._recordingFailureHandled = False
            self._handleRecordingFailure(
                message,
                abortManager=True,
            )
            return False

        try:
            recButtonChecked = bool(
                self._widget.isRecButtonChecked()
            )
        except Exception as error:
            return failLapseEntry(
                'Could not inspect recording-lapse cadence state before the '
                f'next point: {error}'
            )

        if self.stopRequested or not recButtonChecked:
            if (
                not self.stopRequested
                and self.__dict__.get('_scanStartPublished', False)
                and self.__dict__.get('_scanRequestAccepted', False)
            ):
                return failLapseEntry(
                    'Recording lapse cadence was cancelled while its scan run '
                    'was retained.'
                )
            self._recordingCycleTerminalHandled = False
            self.recordingCycleEnded()
            return False

        # Detailed-signal managers advance only after writer finalization, but
        # retain this defensive drain check for legacy managers and shutdown
        # races. Starting a new timepoint while the previous manager still
        # reports ``record`` would violate its single-session guard.
        try:
            managerStillRecording = bool(
                self._master.recordingManager.record
            )
        except Exception as error:
            return failLapseEntry(
                'Could not inspect recording-manager drain state before the '
                f'next lapse point: {error}'
            )
        if managerStillRecording:
            try:
                timer = Timer(singleShot=True)
                self.timer = timer
                timer.timeout.connect(self.nextLapse)
                timer.start(_LAPSE_RECORDING_DRAIN_RETRY_MS)
            except Exception as error:
                timer = self.__dict__.get('timer')
                if timer is not None:
                    try:
                        timer.stop()
                    except Exception:
                        pass
                self.timer = None
                return failLapseEntry(
                    'Could not schedule recording-manager drain retry before '
                    f'the next lapse point: {error}'
                )
            return True

        self.endedRecording = False
        self.doneScan = False
        self._recordingCycleTerminalHandled = False

        isFirstLapse = self.lapseCurrent == 0
        isFinalLapse = self.lapseCurrent + 1 == self.lapseTotal

        if isFirstLapse:
            if not self._preflightNewScanRequest():
                return False
            resolveSource = getattr(
                self._commChannel, 'getRecordingScanSource', None
            )
            if callable(resolveSource):
                # A timelapse scan is started by the recording, so unlike
                # scan-once there is no operator gesture to infer the scanner
                # from. The chooser is that gesture — and an unmade choice is
                # refused rather than defaulted, because the default would
                # command whichever hardware is registered first.
                selectedKey = self._selectedScanSourceKey()
                if not selectedKey and self._scanSourceChoiceRequired():
                    self._handleRecordingFailure(
                        'Select a scan source in the Recording widget before '
                        'starting a timelapse scan: this setup has more than '
                        'one scan widget that could be driven.',
                        abortManager=False,
                        kind=FailureKind.SCAN,
                    )
                    return False
                try:
                    self._recordingScanSource = resolveSource(selectedKey)
                except Exception as error:
                    self._handleRecordingFailure(
                        str(error), abortManager=False,
                        kind=FailureKind.SCAN,
                    )
                    return False

        if not self.recordingArgs['singleLapseFile']:
            lapseCurrentStr = str(self.lapseCurrent).zfill(len(str(self.lapseTotal)))
            self.recordingArgs['savename'] = f'{self.savename}_scan{lapseCurrentStr}'

        try:
            if isFirstLapse:
                self.recordingArgs['attrs'] = {  # Update
                    detectorName:
                        self._commChannel.sharedAttrs.getHDF5Attributes()
                    for detectorName in self.recordingArgs['detectorNames']
                }
                # Read through the pinned source, never the global accessors:
                # nothing is scanning yet, so a rig with several capable
                # controllers cannot be resolved by the channel.
                if not self._applyScanGeometryToRecordingArgs():
                    return False

            # Set lapse metadata for this timepoint
            self.recordingArgs['recLapseTotal'] = self.lapseTotal
            self.recordingArgs['recLapseIndex'] = self.lapseCurrent
        except Exception as error:
            self._handleRecordingFailure(
                str(error),
                abortManager=bool(
                    getattr(
                        self._master.recordingManager,
                        'record',
                        False,
                    )
                ),
            )
            return False

        # Whoever owns the stage gets to put it where this point belongs
        # before anything is armed. Asked for, not called: the answer arrives
        # on that owner's own thread, and blocking this one would stop the
        # event loop these timepoints are advanced by -- starving, among other
        # things, the focus lock.
        # Looked up rather than called directly: nextLapse is also exercised
        # against lightweight doubles that mirror only the surface they need,
        # and a lapse with no provider is the ordinary timed one.
        await_positioning = getattr(self, '_awaitPositioning', None)
        if callable(await_positioning):
            positioning = await_positioning()
            if positioning is not True:
                return positioning

        # Every timepoint, not just the first. Each one runs its own scan and
        # publishes its own ``sigScanEnded``, so a start published once for the
        # whole lapse leaves every later point ending a scan that never began:
        # the focus lock yields for timepoint one, resumes at its end, and then
        # drives the focus axis straight through timepoint two's waveform. The
        # published flag is cleared as each end arrives, so this stays one
        # start per point rather than one per re-entry of this method.
        #
        # After the positioning gate, deliberately. Placing the sample is not
        # part of the scan, and consumers that hold an axis for us should keep
        # holding it while the stage travels -- yielding early would spend the
        # move unlocked for no benefit.
        self._notifyScanStarting()

        if not self._startManagerRecording():
            return False
        if not self._waitForManagerArm():
            return False

        # The source is resolved once before the first timepoint and targeted
        # directly for every continuation. Never broadcast ScanLapse on a
        # standalone setup: several controllers can synchronously command the
        # same firmware before post-dispatch ambiguity is observable.
        if not self._requestScanStart(
            isFirstLapse, not isFinalLapse
        ):
            return False
        return True

    def setPositioningProvider(self, provider) -> None:
        """Let a workflow place the sample before each timepoint.

        ``provider(index)`` returns a :class:`PositioningRequest` that it
        resolves from its own thread once the stage is there and settled. A
        lapse with a provider arms only on ``RESOLVED``; the other three
        terminals end the session with the reason they carry.

        This is what makes a tiling run a lapse: the points differ by where
        the stage is rather than by when the timer fires, and nothing else
        about the session changes.
        """
        self._positioningProvider = provider
        self._pendingPositioning = None

    def clearPositioningProvider(self) -> None:
        self._positioningProvider = None
        self._pendingPositioning = None
        self._cycleTerminalCallback = None

    def setCycleTerminalCallback(self, callback) -> None:
        """Called when a point is wholly finished -- writer *and* scan.

        The manager's writer terminal fires earlier, while scan lifecycle
        cleanup is still running, so a caller that may move hardware must wait
        for this one instead.
        """
        self._cycleTerminalCallback = callback

    def _awaitPositioning(self):
        """True to proceed, False to stop; re-arms the timer while pending."""
        provider = self.__dict__.get('_positioningProvider')
        if provider is None:
            return True

        request = self.__dict__.get('_pendingPositioning')
        if request is None:
            try:
                request = provider(self.lapseCurrent)
            except Exception as error:
                self._handleRecordingFailure(
                    f'Could not request positioning for point '
                    f'{self.lapseCurrent}: {error}',
                    abortManager=False, kind=FailureKind.HARDWARE,
                )
                return False
            if request is None:
                # No positioning wanted for this point.
                return True
            self._pendingPositioning = request

        outcome = request.outcome
        if not outcome.settled:
            # Still moving. Come back to it without holding the event loop,
            # exactly as the drain retry below does.
            try:
                timer = Timer(singleShot=True)
                self.timer = timer
                timer.timeout.connect(self.nextLapse)
                timer.start(_LAPSE_RECORDING_DRAIN_RETRY_MS)
            except Exception as error:
                self._handleRecordingFailure(
                    f'Could not schedule the positioning retry: {error}',
                    abortManager=False, kind=FailureKind.HARDWARE,
                )
                return False
            return False

        self._pendingPositioning = None
        if outcome.mayProceed:
            return True

        # Cancelled, failed or timed out: all three mean this point cannot be
        # acquired where it was meant to be, so none of them may arm.
        self._handleRecordingFailure(
            f'Positioning for point {request.index} '
            f'{outcome.value}: {request.message or "no reason given"}',
            abortManager=False, kind=FailureKind.HARDWARE,
        )
        return False

    def _startManagerRecording(self) -> bool:
        managerGeneration = getattr(
            self._master.recordingManager,
            'recordingGeneration',
            None,
        )
        if isinstance(managerGeneration, int):
            # Open a fresh identity window for every ScanLapse timepoint. A
            # queued terminal from the preceding generation must not be
            # adopted while startRecording is creating the next worker.
            self._recordingGenerationBeforeOperation = managerGeneration
        self._recordingManagerGeneration = None
        try:
            generation = self._master.recordingManager.startRecording(
                **self.recordingArgs
            )
            if isinstance(generation, int) and not isinstance(
                generation, bool
            ):
                self._recordingManagerGeneration = generation
            else:
                managerGeneration = getattr(
                    self._master.recordingManager,
                    'recordingGeneration',
                    None,
                )
                if isinstance(managerGeneration, int):
                    self._recordingManagerGeneration = managerGeneration
            baseline = self.__dict__.get(
                '_recordingGenerationBeforeOperation'
            )
            if (
                self.__dict__.get('_usesDetailedRecordingSignals', False)
                and isinstance(baseline, int)
                and (
                    not isinstance(self._recordingManagerGeneration, int)
                    or self._recordingManagerGeneration <= baseline
                )
            ):
                raise RuntimeError(
                    'Recording manager did not create a new session identity.'
                )
            return True
        except Exception as error:
            self._handleRecordingFailure(
                str(error),
                abortManager=bool(
                    getattr(
                        self._master.recordingManager,
                        'record',
                        False,
                    )
                ),
            )
            return False

    def _waitForManagerArm(self) -> bool:
        try:
            armed = self._master.recordingManager.waitForAcquisitionStarted(
                RECORDING_ARM_TIMEOUT
            )
        except Exception as error:
            self._handleRecordingFailure(str(error), abortManager=True,
                                         kind=FailureKind.HARDWARE)
            return False
        if armed:
            return True
        self._handleRecordingFailure(
            f'Detectors did not report armed within '
            f'{RECORDING_ARM_TIMEOUT:.1f}s; scan was not started.',
            abortManager=True,
        )
        return False

    def _applyScanGeometryToRecordingArgs(self) -> bool:
        """Read the scan geometry into ``recordingArgs``. False on failure.

        The accessors resolve through the CommunicationChannel, which prefers
        the active scan source over any registration-order guess, so calling
        this once a scanner has announced itself binds the recording to that
        exact scanner.
        """
        try:
            for key, methodName in (
                ('recFrames', 'getNumScanPositions'),
                ('numCamTTL', 'getNumCamTTL'),
            ):
                accessor = self._scanAccessor(methodName)
                if accessor is None:
                    raise RuntimeError(
                        f'The scan source for this recording does not provide '
                        f'{methodName}(), so the number of frames to record '
                        f'cannot be determined.'
                    )
                self.recordingArgs[key] = accessor()
            # Optional: absent on scan sources that are not BeadRec-capable.
            # A recording without these is uncalibrated, not wrong.
            self.recordingArgs['scanDims'] = self._scanDimsForRecording()
            self.recordingArgs['scanStepSizes'] = (
                self._scanStepSizesForRecording()
            )
        except Exception as error:
            self._handleRecordingFailure(str(error), abortManager=False)
            return False
        return True

    def _scanRunTokenFor(self, source):
        """The coordinator's run reservation held by ``source``, or None."""
        coordinator = getattr(
            self._master, 'scanExecutionCoordinator', None
        )
        runForOwner = getattr(coordinator, 'runForOwner', None)
        if not callable(runForOwner):
            return None
        try:
            return runForOwner(source)
        except Exception:
            return None

    def _cancelPendingScanSourceArm(self) -> None:
        """Tear down a scan-once arm that no scan ever claimed.

        The manager was never handed a session, so ``endRecording()`` produces
        no terminal to run the ordinary cleanup. Close the operation locally
        instead, clearing the pending arm first so a scan started during
        teardown cannot bind to a recording that is going away.
        """
        self._awaitingScanSourceArm = False
        self._recordingScanSource = None
        self.recordingCycleEnded()

    def prepareForScanSource(self, source) -> bool:
        """Bind an armed scan-once recording to the scan about to start.

        Called synchronously by the scan controller before it announces itself
        or touches hardware, so a failure here can still stop the scan. False
        means "do not start": a scan that ran anyway would bleach the sample
        with nothing recording, and the geometry it was armed with would be
        another scanner's.
        """
        if source is None or not self._awaitingScanSourceArm:
            return True
        self._awaitingScanSourceArm = False
        self._recordingScanSource = source
        # The source reserves its run before asking, so pin the exact
        # reservation now: a later abort must target this run and not whichever
        # generation that controller happens to be running by then.
        self._acceptedScanRunToken = self._scanRunTokenFor(source)
        self._scanRequestAccepted = True
        if not self._applyScanGeometryToRecordingArgs():
            return False
        if not self._startManagerRecording():
            return False
        if not self._waitForManagerArm():
            return False
        # The scan source publishes sigScanStarting itself right after this
        # returns, so the run-level lifecycle is pending even though this
        # controller did not open it.
        self._scanStartOwnedByScanSource = True
        self.__logger.info(
            'Scan-once recording bound to the scan source that started: '
            '%s frame(s) expected.',
            self.recordingArgs.get('recFrames'),
        )
        return True

    def _notifyScanStarting(self) -> None:
        if self._scanStartPublished:
            return
        # Mark first so a signal-slot failure can still be paired.
        self._scanStartPublished = True
        self._scanLifecycleEndedObserved = False
        self._commChannel.scanWorkflow.notify_scan_starting()

    def _scanLifecycleEnded(self) -> None:
        expected = self.__dict__.get('_acceptedScanRunToken')
        coordinator = getattr(
            self.__dict__.get('_master'),
            'scanExecutionCoordinator',
            None,
        )
        if expected is not None and coordinator is not None:
            try:
                expectedStillActive = (
                    getattr(coordinator, 'activeRunToken', None) is expected
                )
            except Exception:
                # An unreadable coordinator state is not proof that the exact
                # accepted owner has released its reservation.
                expectedStillActive = True
            heldForThisEndPublication = (
                expectedStillActive
                and bool(getattr(expected, 'releaseRequested', False))
                and bool(getattr(expected, 'releaseBarrierCleared', False))
                and bool(
                    getattr(expected, 'holdReleaseUntilFinalized', False)
                )
            )
            if expectedStillActive and not heldForThisEndPublication:
                # ``sigScanEnded`` is global and queued deliveries can outlive
                # the run that emitted them. The exact token for this recording
                # is still executing, so this event cannot be its terminal. A
                # physically drained token held solely across this synchronous
                # end publication is the one deliberate exception.
                return

        source = self.__dict__.get('_recordingScanSource')
        if (
            expected is None
            and source is not None
            and self.__dict__.get('_scanRequestAccepted', False)
        ):
            try:
                sourceStillRunning = bool(
                    getattr(source, 'isRunning', False)
                )
            except Exception:
                # A malformed adapter must not let a global compatibility
                # terminal clear its still-pinned ownership identity.
                sourceStillRunning = True
            if sourceStillRunning:
                return
        timer = self.__dict__.get('timer')
        timerActive = False
        if timer is not None:
            try:
                timerActive = bool(timer.isActive())
            except Exception:
                timerActive = True
        if (
            (expected is None or coordinator is None)
            and source is not None
            and self.__dict__.get('_scanRequestAccepted', False)
            and self.recMode == RecMode.ScanLapse
            and timerActive
            and not self.__dict__.get('stopRequested', False)
        ):
            # A non-final targeted legacy part keeps one run-level lifecycle
            # open while its source is idle between points. The same is true for
            # an exact third-party adapter whose request token is not registered
            # in the shared coordinator: without owner identity, a foreign
            # queued global end must not close that retained run.
            return

        exactCompletion = self.__dict__.get(
            '_acceptedScanCompletion'
        )
        if exactCompletion is not None:
            # A controller publishes sigScanEnded immediately before resolving
            # its exact request terminal. Record the lifecycle observation but
            # leave success/failure and cadence advancement to that identity-
            # scoped terminal rather than this global compatibility signal.
            # The active-token checks above remain essential: a stale global
            # end must not be attributed to the current exact request.
            self._scanLifecycleEndedObserved = True
            self._scanStartPublished = False
            if (
                self.__dict__.get(
                    '_exactScanCompletionHandled', False
                )
                and (
                    self.endedRecording
                    or not self.__dict__.get(
                        '_usesDetailedRecordingSignals', False
                    )
                )
            ):
                self.doneScan = True
                self.recordingCycleEnded()
            return

        # A late-bound scan-once recording never published the start itself —
        # its scan source did — so the terminal checks below must treat that
        # as just as pending, or a scanner that fails after the manager was
        # armed leaves the writer waiting for frames until its stall watchdog.
        wasPending = (
            self._scanStartPublished or self._scanStartOwnedByScanSource
        )
        accepted = self._scanRequestAccepted
        if accepted:
            self._scanLifecycleEndedObserved = True
        self._scanStartPublished = False
        self._scanStartOwnedByScanSource = False
        self._scanRequestAccepted = False
        self._acceptedScanRunToken = None
        if (
            (self.__dict__.get('stopRequested', False)
             or self.__dict__.get('_shutdownRequested', False))
            and accepted
        ):
            self.doneScan = True
            if (
                self.endedRecording
                or not self.__dict__.get(
                    '_usesDetailedRecordingSignals', False
                )
            ):
                self.recordingCycleEnded()
            return
        if (
            wasPending
            and accepted
            and self.recMode in (RecMode.ScanOnce, RecMode.ScanLapse)
            and not self.doneScan
            and not self._recordingFailureHandled
        ):
            # A build/arm failure has no sigScanDone. Without this terminal
            # check, Recording stays armed until its frame-stall watchdog even
            # though the scan controller has already rejected the run.
            self._handleRecordingFailure(
                'Scan ended before acquisition completed.',
                abortManager=True,
            )

    def _notifyScanEndedIfPending(self) -> None:
        if not self._scanStartPublished:
            return
        self._scanStartPublished = False
        try:
            self._commChannel.scanWorkflow.notify_scan_ended()
        except Exception:
            # Writer teardown must continue, but a failed publication is not a
            # paired terminal. Restore the marker so shutdown stays fail-closed
            # and a later close retry can publish the end again.
            self._scanStartPublished = True
            self.__logger.error(
                'Failed to publish the recording-owned scan lifecycle end',
                exc_info=True,
            )

    def _scanRunIsActive(self) -> bool:
        coordinator = getattr(
            self._master, 'scanExecutionCoordinator', None
        )
        expected = self.__dict__.get('_acceptedScanRunToken')
        exactCompletion = self.__dict__.get('_acceptedScanCompletion')
        if (
            exactCompletion is not None
            and not self.__dict__.get(
                '_exactScanCompletionHandled', False
            )
        ):
            # The owner can clear isRunning/the coordinator token before its
            # exact detector-barrier terminal is consumed on the UI thread.
            return True
        if expected is not None:
            if coordinator is not None:
                try:
                    return (
                        getattr(
                            coordinator, 'activeRunToken', None
                        ) is expected
                    )
                except Exception:
                    # Unknown is active: shutdown/failure cleanup must retain
                    # and target the exact accepted owner, never declare idle.
                    return True
            # Some targeted third-party adapters provide an exact request token
            # without using ImSwitch's shared NI coordinator. The pinned source
            # remains an identity-safe fallback in that compatibility case.
            source = self.__dict__.get('_recordingScanSource')
            if (
                source is None
                or not self.__dict__.get(
                    '_scanRequestAccepted', False
                )
            ):
                return False
            try:
                return bool(getattr(source, 'isRunning', False))
            except Exception:
                return True

        source = self.__dict__.get('_recordingScanSource')
        if (
            source is not None
            and self.__dict__.get('_scanRequestAccepted', False)
        ):
            try:
                return bool(getattr(source, 'isRunning', False))
            except Exception:
                return True

        # An unacknowledged legacy broadcast conveys no ownership identity.
        # Never infer abort authority from whichever unrelated run happens to
        # be globally active now.
        return False

    def _finalScanLifecycleEndPending(self) -> bool:
        """Whether this exact part must still close the run-level lifecycle."""
        if not self.__dict__.get('_scanStartPublished', False):
            return False
        if self.recMode == RecMode.ScanOnce:
            return True
        if self.recMode != RecMode.ScanLapse:
            return False
        if (
            self.__dict__.get('stopRequested', False)
            or self.__dict__.get('_shutdownRequested', False)
        ):
            return True
        return self.lapseCurrent + 1 >= self.lapseTotal

    def _preflightNewScanRequest(self) -> bool:
        """Refuse before arming Recording when another scan owns the source."""
        getActiveSource = getattr(
            self._commChannel, 'getActiveScanSource', None
        )
        activeSource = (
            getActiveSource() if callable(getActiveSource) else None
        )
        if activeSource is not None:
            self._handleRecordingFailure(
                'Cannot start scan recording while another scan source is '
                'active.',
                abortManager=False,
            )
            return False

        coordinator = getattr(
            self._master, 'scanExecutionCoordinator', None
        )
        active = (
            getattr(coordinator, 'activeRunToken', None)
            if coordinator is not None else None
        )
        if active is None:
            return True
        self._handleRecordingFailure(
            'Cannot start scan recording while another scan run is active.',
            abortManager=False,
        )
        return False

    def _requestScanStart(self, recalculateSignals: bool,
                          isNonFinalPartOfSequence: bool) -> bool:
        """Request a scan and require an explicit result when supported."""
        source = self.__dict__.get('_recordingScanSource')

        def runTokenForOwner(owner):
            coordinator = getattr(
                getattr(self, '_master', None),
                'scanExecutionCoordinator',
                None,
            )
            try:
                runForOwner = getattr(coordinator, 'runForOwner', None)
            except Exception:
                return None
            if not callable(runForOwner):
                return None
            try:
                return runForOwner(owner)
            except Exception:
                return None

        def retainTargetedAbortAuthority(requestResult=None):
            if source is None:
                return
            token = runTokenForOwner(source)
            if token is None and requestResult is not None:
                try:
                    reportedTokens = [
                        candidate
                        for owner, candidate in tuple(
                            getattr(
                                requestResult, 'acceptedTokens', ()
                            ) or ()
                        )
                        if owner is source and candidate is not None
                    ]
                except Exception:
                    reportedTokens = []
                if len(reportedTokens) == 1:
                    token = reportedTokens[0]
            try:
                sourceRunning = bool(
                    getattr(source, 'isRunning', False)
                )
            except Exception:
                # State inspection failed after targeted dispatch. Retain the
                # exact source as abort authority and treat its state as unknown
                # (therefore active) rather than abandoning armed hardware.
                sourceRunning = True
            if token is not None or sourceRunning:
                self._scanRequestAccepted = True
                self._acceptedScanRunToken = token

        try:
            runFrom = getattr(
                self._commChannel.scanWorkflow, 'run_scan_from', None
            )
            if source is not None:
                if not callable(runFrom):
                    raise RuntimeError(
                        'Targeted scan dispatch is unavailable for the '
                        'selected recording source.'
                    )
                # This controller published the start itself, one per
                # timepoint, and its own bookkeeping pairs it. Letting the
                # dispatcher publish a second one would leave every consumer
                # yielded a level deeper than the single end can unwind.
                result = _dispatchWithoutLifecycle(
                    runFrom,
                    source,
                    recalculateSignals,
                    isNonFinalPartOfSequence,
                )
            else:
                result = _dispatchWithoutLifecycle(
                    self._commChannel.scanWorkflow.run_scan,
                    recalculateSignals,
                    isNonFinalPartOfSequence,
                )
        except Exception as error:
            # The exact targeted source may have partially started before
            # raising. Retain only its identity, never whichever global run is
            # active.
            try:
                attachedResult = getattr(
                    error, 'scanRequestResult', None
                )
            except Exception:
                attachedResult = None
            retainTargetedAbortAuthority(attachedResult)
            self._handleRecordingFailure(str(error), abortManager=True)
            return False

        try:
            handled = bool(getattr(result, 'handled', False))
            accepted = bool(getattr(result, 'accepted', False))
        except Exception as error:
            retainTargetedAbortAuthority(result)
            self._handleRecordingFailure(
                f'Scan controller returned a malformed acceptance envelope: '
                f'{error}',
                abortManager=True,
            )
            return False
        if handled and not accepted:
            retainTargetedAbortAuthority(result)
            try:
                message = str(
                    getattr(
                        result,
                        'rejectionMessage',
                        'No scan controller accepted the recording request.',
                    )
                    or 'No scan controller accepted the recording request.'
                )
            except Exception as error:
                message = (
                    'Scan controller returned a malformed rejection report: '
                    f'{error}'
                )
            self._handleRecordingFailure(message, abortManager=True)
            return False

        if not handled:
            if source is not None:
                # Exact/coordinated sources can already be idle while their
                # run-level reservation is still draining behind detector
                # finalization. Pin that identity before inspecting isRunning
                # so the failure path can still target the real owner instead
                # of synthesizing an early lifecycle end.
                retainTargetedAbortAuthority()
                # A directly invoked standalone controller has no coordinator
                # acknowledgement API. Its synchronous isRunning transition is
                # nevertheless identity-safe because only this exact source
                # was called.
                try:
                    sourceRunning = bool(
                        getattr(source, 'isRunning', False)
                    )
                except Exception as error:
                    self._handleRecordingFailure(
                        'Could not inspect whether the selected scan source '
                        f'started: {error}',
                        abortManager=True,
                    )
                    return False
                if not sourceRunning:
                    self._handleRecordingFailure(
                        'The selected scan source did not start.',
                        abortManager=True,
                    )
                    return False
                try:
                    requiresExactCompletion = bool(
                        getattr(
                            source,
                            'supportsExactScanRequestCompletion',
                            False,
                        )
                    )
                except Exception as error:
                    self._handleRecordingFailure(
                        'Could not inspect the selected scan source completion '
                        f'capability: {error}',
                        abortManager=True,
                    )
                    return False
                if requiresExactCompletion:
                    self._handleRecordingFailure(
                        'The selected exact scan source started without an '
                        'acceptance/completion report.',
                        abortManager=True,
                    )
                    return False
                self._scanRequestAccepted = True
                self._acceptedScanRunToken = None
                return True

            # TriggerScope-family/third-party controllers still use the legacy
            # fire-and-forget signal. Preserve that path, but never mistake it
            # for verified ownership or grant it scan-abort authority.
            self.__logger.warning(
                'Scan controller did not provide a start acknowledgement; '
                'continuing in legacy compatibility mode.'
            )
            self._scanRequestAccepted = False
            self._acceptedScanRunToken = None
            return True

        try:
            acceptedTokens = tuple(
                getattr(result, 'acceptedTokens', ()) or ()
            )
            acceptedOwners = [
                owner for owner, wasAccepted, _message
                in tuple(getattr(result, 'reports', ()) or ())
                if wasAccepted
            ]
        except Exception:
            retainTargetedAbortAuthority(result)
            self._handleRecordingFailure(
                'Scan controller returned a malformed acceptance report.',
                abortManager=True,
            )
            return False
        if len(acceptedOwners) != 1:
            retainTargetedAbortAuthority(result)
            self._handleRecordingFailure(
                'A handled scan request must have exactly one accepting '
                'controller.',
                abortManager=True,
            )
            return False
        acceptedOwner = acceptedOwners[0]
        if (
            source is not None
            and acceptedOwner is not source
        ):
            retainTargetedAbortAuthority(result)
            self._handleRecordingFailure(
                'A scan controller other than the selected recording source '
                'acknowledged the request.',
                abortManager=True,
            )
            return False
        try:
            foreignTokens = [
                token for owner, token in acceptedTokens
                if token is not None and owner is not acceptedOwner
            ]
            exactTokens = [
                token for owner, token in acceptedTokens
                if token is not None and owner is acceptedOwner
            ]
        except Exception:
            retainTargetedAbortAuthority(result)
            self._handleRecordingFailure(
                'Scan controller returned a malformed run-identity report.',
                abortManager=True,
            )
            return False
        ownerRunToken = runTokenForOwner(acceptedOwner)
        if foreignTokens or len(exactTokens) > 1:
            retainTargetedAbortAuthority(result)
            self._handleRecordingFailure(
                'Scan controller acknowledged the request with conflicting '
                'run identities.',
                abortManager=True,
            )
            return False
        if len(exactTokens) == 1:
            if (
                ownerRunToken is not None
                and exactTokens[0] is not ownerRunToken
            ):
                retainTargetedAbortAuthority(result)
                self._handleRecordingFailure(
                    'Scan controller reported a run identity that does not '
                    'match its active reservation.',
                    abortManager=True,
                )
                return False
        else:
            # Compatibility with early acknowledgement providers that report
            # an owner but not the token: resolve it through that exact owner,
            # never through the global active-run slot.
            exactTokens = (
                [ownerRunToken] if ownerRunToken is not None else []
            )

        if len(exactTokens) != 1:
            retainTargetedAbortAuthority(result)
            self._handleRecordingFailure(
                'Scan controller acknowledged the request without one '
                'unambiguous run identity.',
                abortManager=True,
            )
            return False

        runToken = exactTokens[0]
        # Preserve abort authority before validating optional capability and
        # completion descriptors: a property getter can itself be broken after
        # the source has already armed hardware.
        self._scanRequestAccepted = True
        self._acceptedScanRunToken = runToken
        try:
            completionEntries = tuple(
                getattr(result, 'acceptedCompletions', ()) or ()
            )
            exactCompletions = [
                completion
                for owner, completionToken, completion in completionEntries
                if (
                    owner is acceptedOwner
                    and completionToken is runToken
                    and completion is not None
                )
            ]
        except Exception:
            retainTargetedAbortAuthority(result)
            self._handleRecordingFailure(
                'Scan controller returned a malformed completion report.',
                abortManager=True,
            )
            return False
        try:
            requiresExactCompletion = bool(
                getattr(
                    acceptedOwner,
                    'supportsExactScanRequestCompletion',
                    False,
                )
            )
        except Exception as error:
            self._handleRecordingFailure(
                f'Could not inspect scan completion capability: {error}',
                abortManager=True,
            )
            return False
        if (
            len(completionEntries) > 1
            or (completionEntries and len(exactCompletions) != 1)
            or (requiresExactCompletion and len(exactCompletions) != 1)
        ):
            self._handleRecordingFailure(
                'Scan controller acknowledged the recording request without '
                'one matching exact completion terminal.',
                abortManager=True,
            )
            return False

        self._acceptedScanCompletion = (
            exactCompletions[0] if exactCompletions else None
        )
        self._exactScanCompletionHandled = False
        completion = self._acceptedScanCompletion
        if completion is not None:
            try:
                completionValid = (
                    getattr(completion, 'owner', None) is acceptedOwner
                    and getattr(completion, 'runToken', None) is runToken
                    and callable(
                        getattr(completion, 'add_done_callback', None)
                    )
                    and callable(getattr(completion, 'wait', None))
                )
            except Exception as error:
                self._handleRecordingFailure(
                    f'Could not inspect exact scan completion terminal: '
                    f'{error}',
                    abortManager=True,
                )
                return False
            if not completionValid:
                self._handleRecordingFailure(
                    'Scan controller reported a mismatched exact completion '
                    'terminal.',
                    abortManager=True,
                )
                return False
            try:
                completion.add_done_callback(
                    lambda _resolved, expected=completion:
                        self._sigExactScanRequestCompleted.emit(expected)
                )
            except Exception as error:
                self._handleRecordingFailure(
                    f'Could not observe exact scan completion: {error}',
                    abortManager=True,
                )
                return False
        return True

    def _exactScanRequestCompleted(self, completion) -> None:
        """Consume only the terminal bound to the currently recorded part."""
        if (
            completion is not self.__dict__.get(
                '_acceptedScanCompletion'
            )
            or self.__dict__.get(
                '_exactScanCompletionHandled', False
            )
        ):
            return
        try:
            if not completion.wait(timeout=0):
                return
        except Exception as error:
            self._exactScanCompletionHandled = True
            self._handleRecordingFailure(
                f'Could not consume exact scan completion: {error}',
                abortManager=True,
            )
            return
        self._exactScanCompletionHandled = True

        try:
            successful = getattr(completion, 'successful', None)
            message = str(getattr(completion, 'message', '') or '')
        except Exception as error:
            self._handleRecordingFailure(
                f'Could not inspect exact scan completion result: {error}',
                abortManager=True,
            )
            return

        if successful is True:
            RecordingController._markRecordingScanCompleted(self)
            self.doneScan = True
            if RecordingController._finalScanLifecycleEndPending(self):
                # Non-final ScanLapse parts deliberately retain one run-level
                # start/token and must advance on their per-part exact terminal.
                # Only ScanOnce, the final lapse part, or an explicit stop waits
                # for the paired run-level end before clearing abort authority.
                return
            if (
                self.__dict__.get(
                    '_recordingFailureAwaitingScanEnd', False
                )
                and not self.__dict__.get(
                    '_scanLifecycleEndedObserved', False
                )
            ):
                return
            if (
                self.endedRecording
                or not self.__dict__.get(
                    '_usesDetailedRecordingSignals', False
                )
            ):
                self.recordingCycleEnded()
            return

        if (
            self.stopRequested
            or self.__dict__.get('_shutdownRequested', False)
        ):
            # A targeted user/shutdown abort is an expected terminal. The
            # writer still owns its own exact generation and may finish after
            # this callback; advance cleanup only when both have completed.
            self.doneScan = True
            if RecordingController._finalScanLifecycleEndPending(self):
                return
            if (
                self.__dict__.get(
                    '_recordingFailureAwaitingScanEnd', False
                )
                and not self.__dict__.get(
                    '_scanLifecycleEndedObserved', False
                )
            ):
                return
            if (
                self.endedRecording
                or not self.__dict__.get(
                    '_usesDetailedRecordingSignals', False
                )
            ):
                self.recordingCycleEnded()
            return

        self._handleRecordingFailure(
            message or 'Scan ended before successful acquisition completion.',
            abortManager=True,
        )

    def _abortOwnedScanSequence(self) -> None:
        source = self.__dict__.get('_recordingScanSource')
        abortFrom = getattr(
            self._commChannel.scanWorkflow, 'abort_scan_from', None
        )
        if source is not None:
            if not callable(abortFrom):
                raise RuntimeError(
                    'Targeted scan abort is unavailable for the selected '
                    'recording source.'
                )
            runToken = self.__dict__.get('_acceptedScanRunToken')
            try:
                inspect.signature(abortFrom).bind(source, runToken)
            except (TypeError, ValueError):
                abortFrom(source)
            else:
                abortFrom(source, runToken)
            return

        abortScan = getattr(
            self._commChannel.scanWorkflow, 'abort_scan', None
        )
        if callable(abortScan):
            abortScan()
        else:
            self._commChannel.sigAbortScan.emit()

    def _handleRecordingFailure(self, message, *, abortManager,
                                kind=None) -> None:
        """Terminalise this recording session.

        ``kind`` is a :class:`FailureKind` describing what went wrong, for
        callers that survive some failures and not others. Omitting it means
        UNKNOWN, which is deliberately not recoverable: a caller deciding
        whether to continue must be told what it would be continuing past,
        and inferring that from the message text would be worse than not
        classifying at all.
        """
        if self._recordingFailureHandled:
            return
        self._recordingFailureHandled = True
        self._recordingFailureAwaitingScanEnd = False
        self._recordingFailedCurrent = True
        self.stopRequested = True
        self.__logger.error('Recording failed: %s', message)

        recordingManager = self._master.recordingManager
        generation = self.__dict__.get('_recordingManagerGeneration')
        reportFailure = getattr(
            recordingManager, 'reportRecordingFailure', None
        )
        if (
            self.__dict__.get('_recordingOperationActive', False)
            and isinstance(generation, int)
            and callable(reportFailure)
        ):
            try:
                reportFailure(message, generation,
                              kind or FailureKind.UNKNOWN)
            except Exception:
                self.__logger.error(
                    'Failed to publish recording-session failure',
                    exc_info=True,
                )

        try:
            if (
                self.recMode in (RecMode.ScanOnce, RecMode.ScanLapse)
                and self._scanStartPublished
            ):
                try:
                    scanRunActive = (
                        self._scanRequestAccepted
                        and self._scanRunIsActive()
                    )
                except Exception:
                    # Unknown is active. Keep the exact source pinned and still
                    # attempt its targeted abort instead of synthesizing an end.
                    scanRunActive = bool(self._scanRequestAccepted)
                    self.__logger.error(
                        'Could not inspect the recording-owned scan state',
                        exc_info=True,
                    )
                # Keep the exact accepted owner pinned until its real run
                # terminal. Otherwise shutdownComplete() and a quick retry can
                # mistake a still-draining detector/NI barrier for an idle scan.
                self._recordingFailureAwaitingScanEnd = scanRunActive
                if self._scanRequestAccepted and scanRunActive:
                    # Release only the run identity accepted for this recording.
                    # A rejected/stale recording failure must never broadcast an
                    # abort into another controller's newer scan.
                    try:
                        self._abortOwnedScanSequence()
                    except Exception:
                        self.__logger.error(
                            'Failed to abort recording-owned scan sequence',
                            exc_info=True,
                        )
                # abort_scan() is a request, not proof that NI-DAQ and detector
                # finish barriers are complete. The owning scan controller
                # emits the real end once its run reservation clears. Only
                # synthesize the pair when there is no active accepted run.
                if not scanRunActive:
                    self._notifyScanEndedIfPending()
        except Exception:
            # No scan-side compatibility failure may skip writer teardown. Keep
            # any accepted identity pinned so shutdown remains fail-closed.
            self._recordingFailureAwaitingScanEnd = bool(
                self._scanRequestAccepted
            )
            self.__logger.error(
                'Failed while terminalizing the recording-owned scan',
                exc_info=True,
            )

        if abortManager:
            try:
                recordingManager.abortRecording(
                    emitSignal=False, wait=True
                )
            except Exception:
                self.__logger.error(
                    'Failed to abort recording after arm failure',
                    exc_info=True,
                )

        self.endedRecording = True
        self.doneScan = True
        if self._recordingFailureAwaitingScanEnd:
            if self.__dict__.get(
                '_scanLifecycleEndedObserved', False
            ):
                self.recordingCycleEnded()
            return
        self.recording = False
        self.recordingCycleEnded()

    def _scanAccessor(self, methodName):
        """Bind a scan accessor to this recording's own source when pinned.

        The CommunicationChannel accessors resolve globally, which is only
        unambiguous while a scan is running. A recording reads its geometry
        before anything starts, so on a rig with several capable scanners the
        global lookup would answer for the wrong one.

        Returns None when a pinned source does not provide the accessor.
        Falling back to the channel there would be worse than having no
        answer: the optional BeadRec accessors exist on the raster controller
        only, so a pinned RESOLFT scan would silently be labelled with the
        raster's dimensions and step sizes.
        """
        source = self.__dict__.get('_recordingScanSource')
        if source is not None:
            accessor = getattr(source, methodName, None)
            return accessor if callable(accessor) else None
        return getattr(self._commChannel, methodName, None)

    def _scanDimsForRecording(self):
        """(Nx, Ny, Nz) scan pixel counts for OME axis labeling (None if no scan)."""
        accessor = self._scanAccessor('getDimsScan')
        if accessor is None:
            return None
        try:
            return tuple(int(d) for d in accessor())
        except Exception:
            return None

    def _scanStepSizesForRecording(self):
        """Scan step sizes matching getDimsScan(), used for OME calibration."""
        accessor = self._scanAccessor('getScanStepSizes')
        if accessor is None:
            return None
        try:
            return tuple(float(s) for s in accessor())
        except Exception:
            return None

    def recordingStarted(self):
        self._widget.setFieldsEnabled(False)

    def _recordingSignalMatches(self, generation) -> bool:
        if not self.__dict__.get('_recordingOperationActive', False):
            return False
        expected = self.__dict__.get('_recordingManagerGeneration')
        if expected is None:
            current = getattr(
                self._master.recordingManager,
                'recordingGeneration',
                None,
            )
            baseline = self.__dict__.get(
                '_recordingGenerationBeforeOperation'
            )
            if (
                current == generation
                and (
                    not isinstance(baseline, int)
                    or generation > baseline
                )
            ):
                self._recordingManagerGeneration = generation
                return True
        return expected == generation

    def _recordingStartedDetailed(self, generation):
        if self._recordingSignalMatches(generation):
            self.recordingStarted()

    def _recordingEndedDetailed(self, generation):
        if self._recordingSignalMatches(generation):
            self.recordingEnded()

    def _recordingFailedDetailed(self, message, generation):
        if self._recordingSignalMatches(generation):
            self.recordingFailed(message)

    def recordingCycleEnded(self):
        if self.__dict__.get(
            '_recordingCycleTerminalHandled', False
        ):
            return
        self._recordingCycleTerminalHandled = True

        # The point is finished here and not before: this terminal is reached
        # only once the writer has drained *and* the scan has completed. A
        # workflow that moves the stage on the writer terminal alone would move
        # it while scan lifecycle cleanup was still running.
        onCycleTerminal = self.__dict__.get('_cycleTerminalCallback')
        if callable(onCycleTerminal):
            try:
                onCycleTerminal()
            except Exception:
                self.__logger.error(
                    'Recording cycle-terminal callback failed', exc_info=True
                )

        scheduleNext = False
        nextDelayMs = 0
        isCameraLapse = self.recMode == RecMode.CameraLapse
        hasMoreScanLapsePoints = (
            not self.__dict__.get('_shutdownRequested', False)
            and self.recMode == RecMode.ScanLapse
            and not self.stopRequested
            and 0 < self.lapseCurrent + 1 < self.lapseTotal
        )
        hasMoreCameraLapsePoints = (
            not self.__dict__.get('_shutdownRequested', False)
            and isCameraLapse
            and not self.stopRequested
            and 0 <= self.lapseCurrent
            and self.lapseCurrent + 1 < self.lapseTotal
        )
        cadenceFailure = None
        if hasMoreScanLapsePoints:
            try:
                scheduleNext = bool(
                    self._widget.isRecButtonChecked()
                )
                if not scheduleNext:
                    cadenceFailure = (
                        'Recording lapse cadence was cancelled before the '
                        'retained scan run reached its final part.'
                    )
                else:
                    nextDelayMs = int(
                        self._widget.getTimelapseFreq() * 1000
                    )
            except Exception as error:
                cadenceFailure = (
                    'Could not inspect recording-lapse cadence state while its '
                    f'scan run was retained: {error}'
                )
                scheduleNext = False
        elif hasMoreCameraLapsePoints:
            try:
                scheduleNext = bool(
                    self._widget.isRecButtonChecked()
                )
                if not scheduleNext:
                    cadenceFailure = (
                        'Camera timelapse was cancelled before all requested '
                        'timepoints completed.'
                    )
            except Exception as error:
                cadenceFailure = (
                    'Could not inspect camera-timelapse cadence state: '
                    f'{error}'
                )
                scheduleNext = False

        if cadenceFailure is not None:
            # ScanLapse may retain a run-level owner; CameraLapse retains only
            # logical controller state. Both still require one authoritative
            # failure cleanup.
            self._recordingCycleTerminalHandled = False
            self._recordingFailureHandled = False
            self._handleRecordingFailure(
                cadenceFailure,
                abortManager=True,
            )
            return

        if scheduleNext:
            completedCameraFrames = (
                self.lapseCurrent + 1 if isCameraLapse else None
            )
            self.lapseCurrent += 1
            self._acceptedScanCompletion = None
            self._exactScanCompletionHandled = False
            self._scanLifecycleEndedObserved = False
            self._recordingFailureHandled = False
            self._recordingFailureAwaitingScanEnd = False
            self._recordingFailedCurrent = False
            try:
                if isCameraLapse:
                    self._widget.updateCameraLapseNum(
                        completedCameraFrames
                    )
                else:
                    self._widget.updateRecLapseNum(self.lapseCurrent)
            except Exception:
                self.__logger.error(
                    'Failed to update recording-lapse progress',
                    exc_info=True,
                )
            try:
                if isCameraLapse:
                    interval = float(self._cameraLapseIntervalS)
                    now = time.monotonic()
                    previousDeadline = self.__dict__.get(
                        '_cameraLapseNextDeadline'
                    )
                    if previousDeadline is None:
                        nextDeadline = now + interval
                    else:
                        nextDeadline = previousDeadline + interval
                        # A delayed Qt callback, suspended workstation, or a
                        # capture taking longer than the interval must never
                        # cause rapid catch-up frames.
                        if interval > 0 and nextDeadline <= now:
                            nextDeadline = now + interval
                        elif interval == 0:
                            nextDeadline = now
                    self._cameraLapseNextDeadline = nextDeadline
                    remaining = max(0.0, nextDeadline - now)
                    self._cameraLapsePlannedStart = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=remaining)
                    )
                    self._scheduleCameraLapseTimer()
                else:
                    timer = Timer(singleShot=True)
                    self.timer = timer
                    timer.timeout.connect(self.nextLapse)
                    timer.start(nextDelayMs)
            except Exception as error:
                timer = self.__dict__.get('timer')
                if timer is not None:
                    try:
                        timer.stop()
                    except Exception:
                        pass
                self.timer = None
                # Re-open both guards so the scheduling failure can drive the
                # ordinary targeted scan + writer failure terminal.
                self._recordingCycleTerminalHandled = False
                self._recordingFailureHandled = False
                self._handleRecordingFailure(
                    f'Could not schedule the next '
                    f'{"camera-" if isCameraLapse else "recording-"}lapse '
                    f'point: '
                    f'{error}',
                    abortManager=True,
                )
            return

        emitRecordingEnded = (
            self.recMode == RecMode.ScanLapse
            and self.stopRequested
            and not self.__dict__.get(
                '_recordingFailedCurrent', False
            )
        )

        # Clear authoritative operation/ownership state before best-effort UI
        # work. A deleted widget or plugin signal must never make this terminal
        # permanently unrepeatable with a live accepted owner still pinned.
        timer = self.__dict__.get('timer')
        self.timer = None
        self.recording = False
        self.lapseCurrent = -1
        self.stopRequested = False
        self._scanRequestAccepted = False
        self._acceptedScanRunToken = None
        self._acceptedScanCompletion = None
        self._exactScanCompletionHandled = False
        self._scanLifecycleEndedObserved = False
        self._recordingScanSource = None
        self._awaitingScanSourceArm = False
        self._scanStartOwnedByScanSource = False
        self._recordingOperationActive = False
        self._recordingManagerGeneration = None
        self._recordingGenerationBeforeOperation = None
        self._recordingFailureAwaitingScanEnd = False
        self._recordingFailedCurrent = False
        self._cameraLapseIntervalS = None
        self._cameraLapseNextDeadline = None
        self._cameraLapsePlannedStart = None

        if timer is not None:
            try:
                timer.stop()
            except Exception:
                self.__logger.error(
                    'Failed to stop the recording-lapse timer during cleanup',
                    exc_info=True,
                )

        cleanupOperations = [
            (
                lambda: self._widget.updateRecFrameNum(0),
                'reset recording frame progress',
            ),
            (
                lambda: self._widget.updateRecTime(0),
                'reset recording time progress',
            ),
            (
                lambda: self._widget.updateRecLapseNum(0),
                'reset recording-lapse progress',
            ),
        ]
        updateCameraLapseNum = getattr(
            self._widget, 'updateCameraLapseNum', None
        )
        if callable(updateCameraLapseNum):
            cleanupOperations.append(
                (
                    lambda: updateCameraLapseNum(0),
                    'reset camera-timelapse progress',
                )
            )
        for operation, description in cleanupOperations:
            try:
                operation()
            except Exception:
                self.__logger.error(
                    'Failed to %s during recording cleanup',
                    description,
                    exc_info=True,
                )

        self._finalizingRecCycle = True
        try:
            try:
                self._widget.setRecButtonChecked(False)
            except Exception:
                self.__logger.error(
                    'Failed to reset the recording button during cleanup',
                    exc_info=True,
                )
        finally:
            self._finalizingRecCycle = False

        try:
            self._widget.setFieldsEnabled(True)
        except Exception:
            self.__logger.error(
                'Failed to enable recording fields during cleanup',
                exc_info=True,
            )

        if emitRecordingEnded:
            try:
                # Emit manually only for a soft ScanLapse stop, because that
                # path never calls recordingManager.endRecording().
                self._commChannel.sigRecordingEnded.emit()
            except Exception:
                self.__logger.error(
                    'Failed to publish the recording-lapse stop terminal',
                    exc_info=True,
                )

    def scanDone(self):
        if self.__dict__.get('_acceptedScanCompletion') is not None:
            # Modern coordinated requests are completed exclusively by their
            # owner/token-scoped terminal. A queued global sigScanDone may
            # belong to an older or standalone scan.
            return
        source = self.__dict__.get('_recordingScanSource')
        sourceStillRunning = False
        if (
            source is not None
            and self.__dict__.get('_scanRequestAccepted', False)
        ):
            try:
                sourceStillRunning = bool(
                    getattr(source, 'isRunning', False)
                )
            except Exception:
                sourceStillRunning = True
        if sourceStillRunning:
            # A targeted legacy source clears isRunning before publishing its
            # own completion. While it is still running, any global done is
            # stale or belongs to another source. Unreadable state likewise
            # fails closed instead of accepting a compatibility success.
            return
        RecordingController._markRecordingScanCompleted(self)
        self.doneScan = True
        if (
            self.__dict__.get(
                '_recordingFailureAwaitingScanEnd', False
            )
            and not self.__dict__.get(
                '_scanLifecycleEndedObserved', False
            )
        ):
            return
        if (
            self.recMode in (RecMode.ScanLapse, RecMode.ScanOnce)
            and (
                self.endedRecording
                or not self.__dict__.get(
                    '_usesDetailedRecordingSignals', False
                )
            )
        ):
            self.recordingCycleEnded()

    def recordingEnded(self):
        self.endedRecording = True
        if self.__dict__.get('_acceptedScanCompletion') is not None:
            if not self.__dict__.get(
                '_exactScanCompletionHandled', False
            ):
                # Global/stale sigScanDone can set doneScan during the narrow
                # arm/dispatch window before the exact terminal is assigned.
                # Once an exact request exists, only that terminal can satisfy
                # the scan side of this recording generation.
                return
            if RecordingController._finalScanLifecycleEndPending(self):
                # The exact request terminal and writer terminal are both
                # necessary, but neither substitutes for the final run-level
                # end paired with the recording's published start.
                return
        if (
            self.__dict__.get(
                '_recordingFailureAwaitingScanEnd', False
            )
            and not self.__dict__.get(
                '_scanLifecycleEndedObserved', False
            )
        ):
            return
        if (
            self.doneScan
            or self.recMode not in (RecMode.ScanLapse, RecMode.ScanOnce)
        ):
            self.recordingCycleEnded()

    def recordingFailed(self, message):
        """Terminal manager failure; reset UI without claiming success."""
        self._handleRecordingFailure(message, abortManager=False)

    def _markRecordingScanCompleted(self):
        """Anchor point-detector watchdogs to this recording's scan terminal."""
        if self.recMode not in (RecMode.ScanOnce, RecMode.ScanLapse):
            return False
        manager = getattr(
            getattr(self, '_master', None), 'recordingManager', None
        )
        if manager is None:
            return False
        marker = getattr(manager, 'markScanCompleted', None)
        if not callable(marker):
            return False
        generation = self.__dict__.get('_recordingManagerGeneration')
        if not isinstance(generation, int):
            generation = getattr(manager, 'recordingGeneration', None)
        if not isinstance(generation, int):
            return False
        try:
            return bool(marker(generation))
        except Exception:
            self.__logger.error(
                'Failed to mark the scan recording complete',
                exc_info=True,
            )
            return False

    def closeEvent(self) -> bool:
        """Cancel lapse callbacks and drain this controller's exact owners."""
        if self.__dict__.get('_shutdownRequested', False):
            if self.__dict__.get('_scanStartPublished', False):
                try:
                    scanRunActive = (
                        self.__dict__.get(
                            '_scanRequestAccepted', False
                        )
                        and self._scanRunIsActive()
                    )
                except Exception:
                    scanRunActive = bool(
                        self.__dict__.get(
                            '_scanRequestAccepted', False
                        )
                    )
                if not scanRunActive:
                    self._notifyScanEndedIfPending()
            return self.shutdownComplete()

        self._shutdownRequested = True
        self.stopRequested = True
        self._recordingCycleTerminalHandled = False
        timer = self.__dict__.get('timer')
        timerWasActive = False
        if timer is not None:
            try:
                timerWasActive = bool(timer.isActive())
            except Exception:
                timerWasActive = True
            try:
                timer.stop()
            except Exception:
                self.__logger.error(
                    'Failed to stop recording-lapse timer during shutdown',
                    exc_info=True,
                )
            self.timer = None

        scanRunActive = (
            self.__dict__.get('_scanRequestAccepted', False)
            and self._scanRunIsActive()
        )
        abortRetainedRun = (
            self.__dict__.get('_scanRequestAccepted', False)
            and (scanRunActive or timerWasActive)
        )
        if abortRetainedRun:
            try:
                self._abortOwnedScanSequence()
            except Exception:
                self.__logger.error(
                    'Failed to abort recording-owned scan during shutdown',
                    exc_info=True,
                )
        if (
            not scanRunActive
            and self.__dict__.get('_scanStartPublished', False)
        ):
            self._notifyScanEndedIfPending()

        try:
            self._master.recordingManager.abortRecording(
                emitSignal=False,
                wait=True,
            )
        except Exception:
            self.__logger.error(
                'Failed to drain recording manager during controller shutdown',
                exc_info=True,
            )

        self.recording = False
        self.endedRecording = True
        if not scanRunActive:
            self.doneScan = True
            self.recordingCycleEnded()
        return self.shutdownComplete()

    def shutdownComplete(self) -> bool:
        """Whether no cadence, writer, or accepted scan owner remains."""
        timer = self.__dict__.get('timer')
        timerActive = False
        if timer is not None:
            try:
                timerActive = bool(timer.isActive())
            except Exception:
                timerActive = True

        recordingManager = getattr(
            self.__dict__.get('_master'),
            'recordingManager',
            None,
        )
        try:
            managerComplete = not bool(
                getattr(recordingManager, 'record', False)
            )
            managerShutdownComplete = getattr(
                recordingManager, 'shutdownComplete', None
            )
        except Exception:
            managerComplete = False
            managerShutdownComplete = None
        if callable(managerShutdownComplete):
            try:
                managerComplete = bool(managerShutdownComplete())
            except Exception:
                managerComplete = False

        return (
            self.__dict__.get('_shutdownRequested', False)
            and not timerActive
            and managerComplete
            and not self._scanRunIsActive()
            and not self.__dict__.get('_scanStartPublished', False)
            and (
                self.__dict__.get('_acceptedScanCompletion') is None
                or self.__dict__.get(
                    '_exactScanCompletionHandled', False
                )
            )
        )

    def updateRecFrameNum(self, recFrameNum):
        if self.recMode == RecMode.SpecFrames:
            self._widget.updateRecFrameNum(recFrameNum)

    def updateRecTime(self, recTime):
        if self.recMode == RecMode.SpecTime:
            self._widget.updateRecTime(recTime)

    def specFrames(self):
        self._widget.checkSpecFrames()
        self._widget.setEnabledParams(specFrames=True)
        self.recMode = RecMode.SpecFrames

    def specTime(self):
        self._widget.checkSpecTime()
        self._widget.setEnabledParams(specTime=True)
        self.recMode = RecMode.SpecTime

    def specLapse(self):
        self._widget.checkSpecLapse()
        self._widget.setEnabledParams(specLapse=True)
        self.recMode = RecMode.CameraLapse

    def recScanOnce(self):
        self._widget.checkScanOnce()
        self._widget.setEnabledParams()
        self.recMode = RecMode.ScanOnce

    def recScanLapse(self):
        self._widget.checkScanLapse()
        self._widget.setEnabledParams(scanLapse=True)
        self._refreshScanSourceOptions()
        self.recMode = RecMode.ScanLapse

    def _refreshScanSourceOptions(self) -> None:
        """Repopulate the timelapse-scan source chooser.

        Refreshed on demand rather than at construction: controllers are built
        one widget at a time, so the registry is still incomplete while this
        controller is being created. The chooser stays hidden unless the rig
        actually has a choice to make.
        """
        listSources = getattr(
            self._commChannel, 'getRecordingScanSourceNames', None
        )
        try:
            sources = list(listSources()) if callable(listSources) else []
        except Exception:
            self.__logger.error(
                'Could not list the scan sources available for recording',
                exc_info=True,
            )
            sources = []
        self._widget.setScanSourceOptions(sources, self._scanSourcePreference)
        # A saved choice for a scanner this setup no longer has must not
        # survive as a preference that later silently rebinds.
        if self._scanSourcePreference not in sources:
            self._scanSourcePreference = ''
        self._widget.setScanSourceVisible(len(sources) > 1)

    def _scanSourceChoiceRequired(self) -> bool:
        """Whether this setup offers a choice the operator has to make.

        One capable scanner is not a choice, so single-scanner rigs are never
        asked and keep resolving automatically.
        """
        listSources = getattr(
            self._commChannel, 'getRecordingScanSourceNames', None
        )
        if not callable(listSources):
            return False
        try:
            return len(list(listSources())) > 1
        except Exception:
            self.__logger.error(
                'Could not list the scan sources available for recording',
                exc_info=True,
            )
            return False

    def _selectedScanSourceKey(self) -> str:
        """The chosen timelapse-scan source, or '' to resolve automatically.

        Falls back to the remembered preference when the chooser has not been
        populated yet, so a restored selection survives a startup in which the
        operator never opened the timelapse mode.
        """
        getSource = getattr(self._widget, 'getScanSource', None)
        if not callable(getSource):
            return self._scanSourcePreference
        try:
            selected = getSource() or ''
        except Exception:
            self.__logger.error(
                'Could not read the selected scan source', exc_info=True
            )
            return self._scanSourcePreference
        if selected:
            self._scanSourcePreference = selected
            return selected
        return self._scanSourcePreference

    def untilStop(self):
        self._widget.checkUntilStop()
        self._widget.setEnabledParams()
        self.recMode = RecMode.UntilStop

    def setRecMode(self, recMode):
        if recMode == RecMode.SpecFrames:
            self.specFrames()
        elif recMode == RecMode.SpecTime:
            self.specTime()
        elif recMode == RecMode.CameraLapse:
            self.specLapse()
        elif recMode == RecMode.ScanOnce:
            self.recScanOnce()
        elif recMode == RecMode.ScanLapse:
            self.recScanLapse()
        elif recMode == RecMode.UntilStop:
            self.untilStop()
        else:
            raise ValueError(f'Invalid RecMode {recMode} specified')

    def detectorChanged(self):
        detectorMode = self._widget.getDetectorMode()
        self._widget.setSpecificDetectorListVisible(detectorMode == -3)
        self._widget.setMultiDetectorSingleFileVisible(detectorMode in [-2, -3])

    def getDetectorNamesToCapture(self):
        """ Returns a list of which detectors the user has selected to be captured. """
        detectorMode = self._widget.getDetectorMode()
        if detectorMode == -1:  # Current detector at start
            return [self._master.detectorsManager.getCurrentDetectorName()]
        elif detectorMode == -2:  # All acquisition detectors
            return list(
                self._master.detectorsManager.execOnAll(
                    lambda c: c.name,
                    condition=lambda c: c.forAcquisition
                ).values()
            )
        elif detectorMode == -3:  # A specific detector
            return self._widget.getSelectedSpecificDetectors()
    
    @APIExport(runOnUIThread=True)
    def getFileName(self):
        """ Gets the filename of the data to save. """
        filename = self._widget.getCustomFilename()
        if filename is None:
            filename = time.strftime('%Hh%Mm%Ss')
        return filename

    def attrChanged(self, key, value):
        if self.settingAttr or len(key) != 2 or key[0] != _attrCategory or value == 'null':
            return

        if key[1] == _recModeAttr:
            if value == 'Snap':
                return
            if value == 'SpecLapse':
                self.specLapse()
                return
            self.setRecMode(RecMode[value])
        elif key[1] == _framesAttr:
            if self.recMode == RecMode.CameraLapse:
                self._widget.setCameraTimelapseNumFrames(value)
            else:
                self._widget.setNumExpositions(value)
        elif key[1] == _timeAttr:
            self._widget.setTimeToRec(value)
        elif key[1] == _lapseTimeAttr:
            self._widget.setTimelapseTime(value)
        elif key[1] == _freqAttr:
            if self.recMode == RecMode.CameraLapse:
                self._widget.setCameraTimelapseInterval(value)
            else:
                self._widget.setTimelapseFreq(value)

    def setSharedAttr(self, attr, value):
        self.settingAttr = True
        try:
            self._commChannel.sharedAttrs[(_attrCategory, attr)] = value
        finally:
            self.settingAttr = False

    def updateRecAttrs(self, *, isSnapping):
        self.setSharedAttr(_framesAttr, 'null')
        self.setSharedAttr(_timeAttr, 'null')
        self.setSharedAttr(_lapseTimeAttr, 'null')
        self.setSharedAttr(_freqAttr, 'null')

        if isSnapping:
            self.setSharedAttr(_recModeAttr, 'Snap')
        else:
            self.setSharedAttr(_recModeAttr, self.recMode.name)
            if self.recMode == RecMode.SpecFrames:
                self.setSharedAttr(_framesAttr, self._widget.getNumExpositions())
            elif self.recMode == RecMode.SpecTime:
                self.setSharedAttr(_timeAttr, self._widget.getTimeToRec())
            elif self.recMode == RecMode.CameraLapse:
                self.setSharedAttr(
                    _framesAttr,
                    self._widget.getTimelapseNumFrames(),
                )
                self.setSharedAttr(
                    _freqAttr,
                    self._widget.getSpecTimelapseFrameTime(),
                )
            elif self.recMode == RecMode.ScanLapse:
                self.setSharedAttr(_lapseTimeAttr, self._widget.getTimelapseTime())
                self.setSharedAttr(_freqAttr, self._widget.getTimelapseFreq())

    def sendScanFreq(self):
        freq = self.getTimelapseFreq()
        self._commChannel.sigSendScanFreq.emit(freq)

    def getTimelapseFreq(self):
        return self._widget.getTimelapseFreq()

    @APIExport(runOnUIThread=True)
    def snapImage(self, output: bool = False) -> Optional[np.ndarray]:
        """ Take a snap and save it to a .tiff file at the set file path. """
        if output:
            return self.snapNumpy()
        else:
            self.snap()

    @APIExport(runOnUIThread=True)
    def startRecording(self) -> None:
        """ Starts recording with the set settings to the set file path. """
        if self.__dict__.get('_shutdownRequested', False):
            return
        self._widget.setRecButtonChecked(True)

    @APIExport(runOnUIThread=True)
    def isRecording(self) -> bool:
        """ Whether a recording is currently active. """
        return bool(self.recording)

    @APIExport(runOnUIThread=True)
    def stopRecording(self) -> bool:
        """ Stops recording. Idempotent: returns True if a recording was
        active and its stop was requested (``recordingEnded`` or
        ``recordingFailed`` will follow), False if nothing was recording (no
        signal will follow, so do not wait for one). """
        wasRecording = bool(self.recording)
        self._widget.setRecButtonChecked(False)
        return wasRecording

    @APIExport(runOnUIThread=True)
    def setRecModeSpecFrames(self, numFrames: int) -> None:
        """ Sets the recording mode to record a specific number of frames. """
        self.specFrames()
        self._widget.setNumExpositions(numFrames)

    @APIExport(runOnUIThread=True)
    def setRecModeSpecTime(self, secondsToRec: Union[int, float]) -> None:
        """ Sets the recording mode to record for a specific amount of time.
        """
        self.specTime()
        self._widget.setTimeToRec(secondsToRec)

    @APIExport(runOnUIThread=True)
    def setRecModeCameraTimelapse(
        self,
        framesToRec: int,
        intervalSeconds: Union[int, float],
        timelapseSingleFile: bool = False,
    ) -> None:
        """Configure a camera-only timelapse of discrete one-frame captures."""
        self.specLapse()
        self._widget.setCameraTimelapseNumFrames(framesToRec)
        self._widget.setCameraTimelapseInterval(intervalSeconds)
        self._widget.setTimelapseSingleFile(timelapseSingleFile)

    @APIExport(runOnUIThread=True)
    def setRecModeScanOnce(self) -> None:
        """ Sets the recording mode to record a single scan. """
        self.recScanOnce()

    @APIExport(runOnUIThread=True)
    def setRecModeScanTimelapse(self, lapsesToRec: int, freqSeconds: float,
                                timelapseSingleFile: bool = False) -> None:
        """ Sets the recording mode to record a timelapse of scans. """
        self.recScanLapse()
        self._widget.setTimelapseTime(lapsesToRec)
        self._widget.setTimelapseFreq(freqSeconds)
        self._widget.setTimelapseSingleFile(timelapseSingleFile)

    @APIExport(runOnUIThread=True)
    def setRecModeUntilStop(self) -> None:
        """ Sets the recording mode to record until recording is manually
        stopped. """
        self.untilStop()

    @APIExport(runOnUIThread=True)
    def setDetectorToRecord(self, detectorName: Union[List[str], str, int],
                            multiDetectorSingleFile: bool = False) -> None:
        """ Sets which detectors to record. One can also pass -1 as the
        argument to record the current detector, or -2 to record all detectors.
        """
        if isinstance(detectorName, int):
            self._widget.setDetectorMode(detectorName)
        else:
            if isinstance(detectorName, str):
                detectorName = [detectorName]
            self._widget.setDetectorMode(-3)
            self._widget.setSelectedSpecificDetectors(detectorName)
            self._widget.setMultiDetectorSingleFile(multiDetectorSingleFile)

    @APIExport(runOnUIThread=True)
    def setRecFilename(self, filename: Optional[str]) -> None:
        """ Sets the name of the file to record to. This only sets the name of
        the file, not the full path. One can also pass None as the argument to
        use a default time-based filename. """
        if filename is not None:
            self._widget.setCustomFilename(filename)
        else:
            self._widget.setCustomFilenameEnabled(False)

    @APIExport(runOnUIThread=True)
    def setRecFolder(self, folderPath: str) -> None:
        """ Sets the folder to save recordings into. """
        self._widget.setRecFolder(folderPath)
    
    @APIExport(runOnUIThread=True)
    def setSpecifyFileName(self,enable=True) -> None:
        self._widget.specifyfile.setChecked(enable)
    
    @APIExport(runOnUIThread=True)
    def setSnapModeSave(self,mode="tiff") -> None:
        self._widget.saveSnapFormatList.setCurrentText(mode)
    
    @APIExport(runOnUIThread=True)
    def getRecFolder(self) -> str:
        return self._widget.folderEdit.text()
    
    # Unified State Persistence Interface (StatefulComponentMixin)
    
    def getComponentState(self) -> dict:
        """Snapshot current recording settings for both startup and setup modes.
        
        Returns recording output settings.
        Does NOT include filename, recording status, or detector/laser selections.
        
        Returns:
            {
                'saveFormat': int,
                'snapSaveMode': int,
                'recSaveMode': int,
                'recFolder': str,
                'recMode': str (enum name),
                'numFrames': int,
                'timeToRec': float,
                'cameraLapseFrames': int,
                'cameraLapseInterval': float,
                'timelapseSingleFile': bool,
                'scanSource': str (scan widget key, '' when unset)
            }
        """
        state = {
            'saveFormat': self._widget.getSaveFormat(),
            'snapSaveMode': self._widget.getSnapSaveMode(),
            'recSaveMode': self._widget.getRecSaveMode(),
            'recFolder': self._widget.getRecFolder(),
            'recMode': self.recMode.name if hasattr(self, 'recMode') else 'UntilStop',
            'numFrames': self._widget.getNumExpositions(),
            'timeToRec': self._widget.getTimeToRec(),
            'cameraLapseFrames': self._widget.getTimelapseNumFrames(),
            'cameraLapseInterval': self._widget.getSpecTimelapseFrameTime(),
            'timelapseSingleFile': self._widget.getTimelapseSingleFile(),
            'scanSource': self._selectedScanSourceKey()
                          or self._scanSourcePreference,
        }

        return state
    
    def applyComponentState(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode
    ) -> list[str]:
        """Restore recording settings from a snapshot.
        
        IDENTICAL behavior in both STARTUP_RESTORE and SETUP_MODE_APPLY:
        - Restore save format and save mode settings
        - Restore recording folder (only if it exists)
        - Restore recording mode (frames vs time)
        - Restore frame count and time values
        
        NEVER (in either mode):
        - Start recording
        - Start acquisition
        - Activate hardware
        
        Per spec Section 0 D2: settings only, never activation.
        
        Args:
            state: Dict returned by getComponentState()
            applyMode: STARTUP_RESTORE or SETUP_MODE_APPLY (no behavioral difference)
        
        Returns:
            List of warning strings (empty if fully successful)
        """
        warnings = []
        
        saveFormat = state.get('saveFormat', SaveFormat.HDF5.value)
        try:
            self._widget.setsaveFormat(saveFormat)
        except Exception as e:
            warnings.append(f'Failed to restore save format: {e}')
        
        snapSaveMode = state.get('snapSaveMode', SaveMode.Disk.value)
        try:
            self._widget.setSnapSaveMode(snapSaveMode)
        except Exception as e:
            warnings.append(f'Failed to restore snap save mode: {e}')
        
        recSaveMode = state.get('recSaveMode', SaveMode.Disk.value)
        try:
            self._widget.setRecSaveMode(recSaveMode)
        except Exception as e:
            warnings.append(f'Failed to restore rec save mode: {e}')
        
        recFolder = state.get('recFolder')
        if recFolder:
            if os.path.exists(recFolder):
                try:
                    self._widget.setRecFolder(recFolder)
                except Exception as e:
                    warnings.append(f'Failed to restore rec folder: {e}')
            else:
                warnings.append(f'Recording folder "{recFolder}" does not exist; skipped.')
        
        numFrames = state.get('numFrames', 100)
        try:
            self._widget.numExpositionsEdit.setText(str(numFrames))
        except Exception as e:
            warnings.append(f'Failed to restore num frames: {e}')
        
        timeToRec = state.get('timeToRec', 1)
        try:
            self._widget.timeToRec.setText(str(timeToRec))
        except Exception as e:
            warnings.append(f'Failed to restore time to record: {e}')

        cameraLapseFrames = state.get('cameraLapseFrames', 5)
        try:
            self._widget.setCameraTimelapseNumFrames(cameraLapseFrames)
        except Exception as e:
            warnings.append(
                f'Failed to restore camera timelapse frame count: {e}'
            )

        cameraLapseInterval = state.get('cameraLapseInterval', 0)
        try:
            self._widget.setCameraTimelapseInterval(cameraLapseInterval)
        except Exception as e:
            warnings.append(
                f'Failed to restore camera timelapse interval: {e}'
            )

        timelapseSingleFile = state.get('timelapseSingleFile', False)
        try:
            self._widget.setTimelapseSingleFile(timelapseSingleFile)
        except Exception as e:
            warnings.append(
                f'Failed to restore timelapse file grouping: {e}'
            )
        
        # Restored before the rec mode: switching to ScanLapse repopulates the
        # chooser, which reapplies this preference to the refreshed entries.
        scanSource = state.get('scanSource', '')
        if isinstance(scanSource, str):
            self._scanSourcePreference = scanSource
        else:
            warnings.append(
                f'Ignored a non-text scan source {scanSource!r}.'
            )

        recModeName = state.get('recMode', 'UntilStop')
        try:
            if recModeName == 'SpecFrames':
                self.specFrames()
            elif recModeName == 'SpecTime':
                self.specTime()
            elif recModeName in ('SpecLapse', 'CameraLapse'):
                # SpecLapse is the saved name used by the previously disabled
                # camera-timelapse prototype.
                self.specLapse()
            elif recModeName == 'ScanOnce':
                self.recScanOnce()
            elif recModeName == 'ScanLapse':
                self.recScanLapse()
            else:
                self.untilStop()
        except Exception as e:
            warnings.append(f'Failed to restore rec mode: {e}')
        
        return warnings
    
    def describeComponentState(self, state: dict) -> list[str]:
        """Generate human-readable summary of saved recording settings.
        
        Args:
            state: Dict returned by getComponentState()
        
        Returns:
            List of formatted strings suitable for setup-mode inspector
        """
        summaries = []
        
        saveFormatVal = state.get('saveFormat')
        if saveFormatVal is not None:
            try:
                formatName = SaveFormat(saveFormatVal).name
            except (ValueError, KeyError):
                formatName = str(saveFormatVal)
            summaries.append(f'  save format: {formatName}')
        
        recMode = state.get('recMode', 'UntilStop')
        summaries.append(f'  recording mode: {recMode}')
        
        if recMode == 'SpecFrames':
            numFrames = state.get('numFrames', 'N/A')
            summaries.append(f'    frames: {numFrames}')
        elif recMode == 'SpecTime':
            timeToRec = state.get('timeToRec', 'N/A')
            summaries.append(f'    duration: {timeToRec} s')
        elif recMode in ('SpecLapse', 'CameraLapse'):
            frames = state.get('cameraLapseFrames', 5)
            interval = state.get('cameraLapseInterval', 0)
            summaries.append(f'    timepoints: {frames}')
            summaries.append(f'    interval: {interval} s')
        
        recFolder = state.get('recFolder')
        if recFolder:
            summaries.append(f'  folder: {recFolder}')
        
        return summaries or ['  no recording settings saved']
    
    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None
    ) -> list[dict]:
        """Identify potential hazards in saved recording state.
        
        Recording settings have no hazards (they do not start recording).
        
        Args:
            state: Dict returned by getComponentState()
            applyMode: STARTUP_RESTORE or SETUP_MODE_APPLY
            context: Optional consumer-provided context (unused)
        
        Returns:
            Empty list (no hazards)
        """
        return []


_attrCategory = 'Rec'
_recModeAttr = 'Mode'
_framesAttr = 'Frames'
_timeAttr = 'Time'
_lapseTimeAttr = 'LapseTime'
_freqAttr = 'LapseFreq'


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
