import time
import os
import sip
import numpy as np
from collections.abc import Callable, Sequence
from threading import Lock
from qtpy import QtCore

from imswitch.imcommon.framework import Thread, Timer, Worker, Signal
from ..basecontrollers import ImConWidgetController, StatefulComponentMixin, ComponentStateApplyMode
from ..display_transform import (
    DisplayTransform,
    apply_display_transform,
    display_transform_from_properties,
)
from tifffile import imread
from imswitch.imcontrol.model.bead_rec_io import (
    oriented_pixel_size, read_pixel_size_um, valid_pixel_size,
    write_reconstruction_tiff,
)
from imswitch.imcontrol.view import guitools
from imswitch.imcontrol.model import getWidgetStatePersistence
from imswitch.imcontrol.model.managers import LeasePurpose
from imswitch.imcontrol.model.bead_recognition import (
    analyze_donut,
    BeadAcquisitionConfig,
    BeadAnalysisParameters,
    BeadRecResultRecord,
    BeadWorkerUpdate,
    ReconstructionUpdate,
    append_roi_means,
    create_reconstruction_buffer,
    find_bead_center,
    fit_bead,
    normalize_roi_bounds,
    reconstruction_image,
    rescale_reconstruction_to_pixel_size,
)

_SCAN_END_DRAIN_TIMEOUT_S = 0.5
_SCAN_END_DRAIN_CHECK_MS = 10
_CLOSE_WAIT_TIMEOUT_MS = 2000


class BeadRecController(ImConWidgetController, StatefulComponentMixin):
    
    componentName = 'BeadRec'
    stateSchemaVersion = 1
    legacyStateNames = ()
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.recIm = None
        self.imDisplay = None
        self.running = False
        self.roiAdded = False
        self.newScan = False
        self.parametersChanged = False
        self.dims = None
        self.stepSizes = None
        self.framesPerPixel = 1
        self.lastDir = None
        self.resultRecords = []
        self.listRecs = []
        self.ongoingScan = False
        self.currentRunImgs = {}
        # Frames delivered by the worker during the current scan; lets
        # onEndedScan distinguish "no data at all" (camera never triggered)
        # from a stale reconstruction left over from a previous scan.
        self.framesReceivedThisScan = 0
        # Set once we've warned that neither a generic 'Scan' widget nor a
        # BeadRec-compatible scan source (e.g. TriggerScope raster) is
        # present, so we don't spam the log every scan.
        self._warnedNoScanWidget = False

        # Gate frame consumption on "armed", set only AFTER the detector buffer
        # is flushed at the real scan start (onNewScan / sigScanStarted) — NOT on
        # raw isScanRunning, which flips True in runScanAdvanced before any scan
        # frame exists. Consuming on the early flag pulled the pre-scan camera
        # backlog into the reconstruction, shifting and inflating it.
        self._scanArmed = False
        # Pre-arm lease + the detector name captured for this scan; see
        # onScanStarting.
        self._scanDetectorHandle = None
        self._scanDetectorName = None
        self._scanDetectorGeneration = None
        self._scanGeneration = 0
        self._chunkConsumerDetectorName = None
        self._chunkConsumerGeneration = None
        self._beadResourceLock = Lock()
        self._beadWorkerGeneration = 0
        self._activeBeadWorkerGeneration = None
        self._beadWorkerStopping = False
        self._drainingEndedScan = False
        self._scanEndDrainGeneration = None
        self._scanEndDrainDeadline = None
        self._scanEndDrainCallback = None
        self._scanEndDrainTimer = Timer(singleShot=True)
        self.beadWorker = BeadWorker(
            isScanRunning=self._scanFramesReady,
            getFrames=self._getCurrentDetectorChunk,
            getRoiBounds=self._getBeadRoiBounds,
        )
        self.beadWorker.sigNewChunk.connect(self.update)
        self.beadWorker.sigWarning.connect(self._logger.warning)
        self.beadWorker.sigWarning.connect(self._widget.setStatusText)
        self.beadWorker.sigProgress.connect(self._updateProgress)
        self.thread = Thread()
        self.beadWorker.moveToThread(self.thread)
        self.thread.started.connect(self.beadWorker.run)
        self.thread.finished.connect(
            self._onBeadWorkerThreadFinished,
            QtCore.Qt.DirectConnection,
        )

        self.yCenter = None
        self.xCenter = None
        self.showCenterState = False
        self.autoAxial=False

        # Connect BeadRecWidget signals
        self._widget.sigROIToggled.connect(self.roiToggled)
        self._widget.sigRunClicked.connect(self.run)
        self._widget.sigScaleClicked.connect(self.updateScaling)
        self._widget.saveRecBtn.clicked.connect(self.saveRec)
        self._widget.loadImgBtn.clicked.connect(self.loadImg)
        self._widget.sigFitRequested.connect(self.runFit)
        self._widget.sigAddCurrentRun.connect(self.addCurrentRun)
        self._widget.sigSelectionChanged.connect(self.selectionChanged)
        self._widget.sigRemoveRecFromList.connect(self.removeRecFromList)
        self._widget.sigClearList.connect(self.clearList)
        self._widget.sigSaveAll.connect(self.saveAll)
        self._widget.sigQueryMousePixelValue.connect(self.updateOnMousePixelValue)

        # Live ROI crop preview: while the ROI is shown and BeadRec is not
        # reconstructing, mirror exactly what the ROI crops from the current
        # detector into the BeadRec viewer, so the user can see what each
        # reconstruction pixel is averaged from before starting a scan.
        self._lastLiveFrame = None
        self._widget.getROIGraphicsItem().sigROIChanged.connect(self._onRoiChanged)
        self._commChannel.sigUpdateImage.connect(self._onLiveImage)

        # Reconstruction orientation correction for the physical scan direction.
        # Applied as the last display step; _orientBase keeps the pre-orientation
        # image so toggling the controls re-renders without recomputing the scan.
        self._orientation = DisplayTransform()
        self._orientBase = None
        self._widget.sigOrientationChanged.connect(self._onOrientationChanged)

        # Connect comm channel signals
        # Pre-arm: sigScanStarting fires BEFORE the scan is built and before
        # any TTL output, whereas sigScanStarted is already too late — a
        # trigger-driven camera would miss the first pulses. Note this is an
        # ARMING requirement, not snapshot membership: BeadRec's camera is
        # free-running/trigger-driven and never joins the scan-driven
        # participant snapshot.
        self._commChannel.sigScanStarting.connect(self.onScanStarting)
        self._commChannel.sigScanStarted.connect(self.updateParameters)
        self._commChannel.sigScanStarted.connect(self.onNewScan)
        self._commChannel.sigScanStarted.connect(self.OngoingScanStatus)
        self._commChannel.sigScanEnded.connect(self.onEndedScan)
        self._commChannel.beadRecWorkflow.on_query_center_coord(self.centerCoordQuery)
        self._commChannel.beadRecWorkflow.on_update_bead_rec_center(self.updateCenterCross)
        self._commChannel.beadRecWorkflow.on_show_bead_rec_center_cross(self.showStateChanged)
        self._commChannel.beadRecWorkflow.on_auto_axial_toggled(self.onAutoAxialToggled)
        self._commChannel.beadRecWorkflow.on_new_axial_list_buffer(self.onNewAxialListBuffer)
        getWidgetStatePersistence().register('BeadRec', self)
        

    def _shutdown(self) -> None:
        """Idempotently stop background work and release scan ownership.

        ``__del__`` can run for a partially initialized controller, so every
        object accessed here is optional. Keeping the actual cleanup in this
        method also lets ``closeEvent`` perform it deterministically instead of
        relying on garbage collection.
        """
        if self.__dict__.get('_shutdownComplete', False):
            return
        self._shutdownComplete = True
        self._disconnectExternalSignals()
        with self._resourceLock():
            self._scanGeneration = (
                self.__dict__.get('_scanGeneration', 0) + 1
            )
            activeWorkerGeneration = self.__dict__.get(
                '_activeBeadWorkerGeneration'
            )
            if activeWorkerGeneration is not None:
                self._beadWorkerStopping = True

        timer = self.__dict__.get('_scanEndDrainTimer')
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass

        worker = self.__dict__.get('beadWorker')
        if worker is not None:
            try:
                worker.stop()
            except Exception:
                pass

        thread = self.__dict__.get('thread')
        workerStopped = True
        if thread is not None:
            try:
                thread.quit()
                workerStopped = self._waitForWorkerThread(thread)
            except Exception:
                workerStopped = False

        if workerStopped:
            self._onBeadWorkerThreadFinished(activeWorkerGeneration)
            # A partially initialized controller may never have started a
            # worker generation, but can still own a pre-arm lease.
            self._releaseBeadDetectorResources()
        else:
            logger = self.__dict__.get('_logger')
            if logger is not None:
                logger.warning(
                    'BeadRec worker is still stopping after widget close; '
                    'its detector resources will be released when the worker '
                    'exits.'
                )

    def _waitForWorkerThread(self, thread) -> bool:
        """Bound GUI waits while retaining resources for a late worker exit."""
        if isinstance(thread, QtCore.QThread):
            if sip.isdeleted(thread):
                return True
            try:
                return bool(
                    QtCore.QThread.wait(thread, _CLOSE_WAIT_TIMEOUT_MS)
                )
            except RuntimeError:
                if sip.isdeleted(thread):
                    return True
                raise
        # Lightweight test/fallback thread implementations are expected to
        # provide a synchronous wait.
        result = thread.wait()
        return True if result is None else bool(result)

    def _resourceLock(self):
        lock = self.__dict__.get('_beadResourceLock')
        if lock is None:
            lock = Lock()
            self._beadResourceLock = lock
        return lock

    @staticmethod
    def _workerThreadIsRunning(thread) -> bool:
        if thread is None:
            return False
        if isinstance(thread, QtCore.QThread):
            if sip.isdeleted(thread):
                return False
            try:
                return bool(thread.isRunning())
            except RuntimeError:
                return not sip.isdeleted(thread)
        isAlive = getattr(thread, 'is_alive', None)
        if isAlive is not None:
            return bool(isAlive())
        isRunning = getattr(thread, 'isRunning', None)
        if isRunning is not None:
            return bool(isRunning())
        return False

    def shutdownComplete(self) -> bool:
        """Return whether close-time worker/resource cleanup has finished."""
        with self._resourceLock():
            stopping = self.__dict__.get('_beadWorkerStopping', False)
            handle = self.__dict__.get('_scanDetectorHandle')
        return (
            not stopping
            and handle is None
            and not self._workerThreadIsRunning(self.__dict__.get('thread'))
        )

    def _onBeadWorkerThreadFinished(self, expectedGeneration=None) -> None:
        """Finalize only the worker generation that actually stopped."""
        with self._resourceLock():
            activeGeneration = self.__dict__.get(
                '_activeBeadWorkerGeneration'
            )
            if (
                expectedGeneration is not None
                and activeGeneration is not None
                and expectedGeneration != activeGeneration
            ):
                return
            if activeGeneration is not None:
                self._activeBeadWorkerGeneration = None
            self._beadWorkerStopping = False
            scanGeneration = self.__dict__.get('_scanDetectorGeneration')
        self._releaseBeadDetectorResources(scanGeneration)

    def _releaseBeadDetectorResources(self, expectedGeneration=None) -> None:
        """Release resources only after the frame-reading worker has stopped."""
        try:
            self._releaseDetectorChunkConsumer(expectedGeneration)
        except Exception as e:
            logger = self.__dict__.get('_logger')
            if logger is not None:
                logger.error(
                    f'BeadRec failed to release its detector frame queue: {e}',
                    exc_info=True,
                )
        self._releaseScanDetectorLease(expectedGeneration)

    @staticmethod
    def _disconnectSignal(signal, slot) -> None:
        if signal is None:
            return
        try:
            signal.disconnect(slot)
        except Exception:
            # Qt raises when a slot was never connected or its owner was
            # already destroyed. Both are safe during idempotent teardown.
            pass

    def _disconnectExternalSignals(self) -> None:
        """Prevent global/timer/worker callbacks after widget teardown."""
        comm = self.__dict__.get('_commChannel')
        if comm is not None:
            for signalName, slot in (
                ('sigUpdateImage', self._onLiveImage),
                ('sigScanStarting', self.onScanStarting),
                ('sigScanStarted', self.updateParameters),
                ('sigScanStarted', self.onNewScan),
                ('sigScanStarted', self.OngoingScanStatus),
                ('sigScanEnded', self.onEndedScan),
                ('sigQueryCenterCoord', self.centerCoordQuery),
                ('sigUpdateBeadRecCenter', self.updateCenterCross),
                ('sigShowBeadRecCenterCross', self.showStateChanged),
                ('sigAutoAxialToggled', self.onAutoAxialToggled),
                ('sigNewAxialListBuffer', self.onNewAxialListBuffer),
            ):
                self._disconnectSignal(getattr(comm, signalName, None), slot)

        timer = self.__dict__.get('_scanEndDrainTimer')
        if timer is not None:
            callback = self.__dict__.get('_scanEndDrainCallback')
            self._disconnectSignal(
                getattr(timer, 'timeout', None), callback
            )

        worker = self.__dict__.get('beadWorker')
        if worker is not None:
            self._disconnectSignal(
                getattr(worker, 'sigNewChunk', None), self.update
            )
            logger = self.__dict__.get('_logger')
            if logger is not None:
                self._disconnectSignal(
                    getattr(worker, 'sigWarning', None), logger.warning
                )
            widget = self.__dict__.get('_widget')
            if widget is not None:
                self._disconnectSignal(
                    getattr(worker, 'sigWarning', None), widget.setStatusText
                )
            self._disconnectSignal(
                getattr(worker, 'sigProgress', None), self._updateProgress
            )

    def closeEvent(self) -> bool:
        self._shutdown()
        super().closeEvent()
        return self.shutdownComplete()

    def __del__(self) -> None:
        try:
            self._shutdown()
        except Exception:
            pass
        try:
            parentDel = getattr(super(), '__del__', None)
            if parentDel is not None:
                parentDel()
        except Exception:
            pass

    # readChunk consumer key — getChunk() is a destructive read, and the
    # RecordingManager polls the same detector during scan-once recordings;
    # readChunk distributes every frame to both consumers.
    _CHUNK_CONSUMER = 'BeadRec'

    def _reconstructionDetectorName(self):
        """The detector this scan reconstructs from.

        Pinned at pre-arm (onScanStarting) and held for the whole scan:
        resolving the *current* detector per chunk means switching the view's
        detector mid-scan silently switches the reconstruction source, mixing
        two cameras' frames into one reconstruction. Falls back to the current
        detector outside a scan.
        """
        with self._resourceLock():
            detectorName = self.__dict__.get('_scanDetectorName')
        if detectorName is not None:
            return detectorName
        return self._master.detectorsManager.getCurrentDetectorName()

    def _getCurrentDetectorChunk(self) -> Sequence[np.ndarray]:
        with self._resourceLock():
            detectorName = self.__dict__.get('_scanDetectorName')
            detectorGeneration = self.__dict__.get(
                '_scanDetectorGeneration'
            )
            if (
                detectorName is None
                or self.__dict__.get('_scanDetectorHandle') is None
            ):
                return []
            self._chunkConsumerDetectorName = detectorName
            self._chunkConsumerGeneration = detectorGeneration
            # Keep resource cleanup out until the destructive hardware drain
            # has completed. Otherwise cleanup can release the last detector
            # lease in this gap and this call would re-register a consumer on
            # already-stopped hardware.
            chunk = self._master.detectorsManager.execOn(
                detectorName,
                lambda c: c.readChunk(self._CHUNK_CONSUMER),
            )
        # The ROI is drawn on the DISPLAYED image (rotated/flipped per the
        # detector's display transform), but readChunk returns raw frames.
        # Apply the same transform so the ROI bounds index the region the user
        # actually selected — otherwise the reconstruction averages a shifted
        # region on cameras with a non-trivial display orientation.
        return self._toDisplayedFrames(chunk)

    def _currentDisplayTransform(self) -> DisplayTransform:
        try:
            # Same pinned detector as the frames themselves, so the transform
            # can never be read off a different camera than the one supplying
            # the chunk.
            name = self._reconstructionDetectorName()
            info = self._setupInfo.detectors.get(name)
        except Exception:
            return DisplayTransform()
        return display_transform_from_properties(
            info.managerProperties if info is not None else None
        )

    def _toDisplayedFrames(self, frames: Sequence[np.ndarray]) -> Sequence[np.ndarray]:
        transform = self._currentDisplayTransform()
        if transform.is_identity:
            return frames
        return [
            apply_display_transform(np.asarray(f), None, transform)[0]
            for f in frames
        ]

    def _releaseDetectorChunkConsumer(self, expectedGeneration=None) -> None:
        """Release BeadRec's queue only on the detector it consumed from."""
        with self._resourceLock():
            generation = self.__dict__.get('_chunkConsumerGeneration')
            if (
                expectedGeneration is not None
                and generation is not None
                and generation != expectedGeneration
            ):
                return
            detectorName = self.__dict__.get('_chunkConsumerDetectorName')
            self._chunkConsumerDetectorName = None
            self._chunkConsumerGeneration = None
        if detectorName is None:
            return
        self._master.detectorsManager.execOn(
            detectorName,
            lambda c: c.releaseChunkConsumer(self._CHUNK_CONSUMER),
        )

    def onScanStarting(self) -> None:
        """Pre-arm the reconstruction camera, before the scan is built.

        Declared here rather than at sigScanStarted so the detector is armed
        before any TTL output: a trigger-driven camera armed at scan start has
        already missed the first pulses, which shows up as a reconstruction
        short by a few frames or shifted by one.

        The detector name is captured ONCE per scan and held for the whole
        run. Reading ``execOnCurrent`` per frame instead would silently switch
        reconstruction sources mid-scan if the user changed the current
        detector in the view.
        """
        if (
            self.__dict__.get('_shutdownComplete', False)
            or not self._widget.runButton.isChecked()
        ):
            return
        self._cancelScanEndDrain()
        self._scanArmed = False
        self._releaseBeadDetectorResources()
        with self._resourceLock():
            generation = self.__dict__.get('_scanGeneration', 0) + 1
            self._scanGeneration = generation
        try:
            detectorName = self._master.detectorsManager.getCurrentDetectorName()
        except Exception as e:
            self._logger.error(
                f'BeadRec could not select a detector for the scan: {e}',
                exc_info=True,
            )
            return
        try:
            handle = self._master.detectorsManager.acquire(
                [detectorName], LeasePurpose.WORKFLOW
            )
        except Exception as e:
            self._logger.error(
                f'BeadRec could not arm detector "{detectorName}" for the '
                f'scan: {e}', exc_info=True
            )
            try:
                self._widget.setStatusText(
                    f'BeadRec could not arm "{detectorName}"; '
                    'this scan will not be reconstructed'
                )
            except Exception:
                pass
            return

        stale = False
        with self._resourceLock():
            if (
                self.__dict__.get('_scanGeneration') != generation
                or self.__dict__.get('_shutdownComplete', False)
                or not self._widget.runButton.isChecked()
            ):
                stale = True
            else:
                self._scanDetectorHandle = handle
                self._scanDetectorName = detectorName
                self._scanDetectorGeneration = generation
        if stale:
            try:
                self._master.detectorsManager.release(handle)
            except Exception as e:
                self._logger.error(
                    f'BeadRec failed to release a stale detector lease: {e}',
                    exc_info=True,
                )

    def _releaseScanDetectorLease(self, expectedGeneration=None) -> None:
        with self._resourceLock():
            generation = self.__dict__.get('_scanDetectorGeneration')
            if (
                expectedGeneration is not None
                and generation is not None
                and generation != expectedGeneration
            ):
                return
            handle = self.__dict__.get('_scanDetectorHandle')
            self._scanDetectorHandle = None
            self._scanDetectorName = None
            self._scanDetectorGeneration = None
        if handle is None:
            return
        try:
            self._master.detectorsManager.release(handle)
        except Exception as e:
            self._logger.error(
                f'BeadRec failed to release its scan detector lease: {e}',
                exc_info=True,
            )

    def _hasScanDetectorLease(self) -> bool:
        with self._resourceLock():
            return (
                self.__dict__.get('_scanDetectorHandle') is not None
                and self.__dict__.get('_scanDetectorName') is not None
                and self.__dict__.get('_scanDetectorGeneration')
                == self.__dict__.get('_scanGeneration')
            )

    def _scanFramesReady(self) -> bool:
        """Whether the worker may consume detector frames as scan pixels.

        True only once the scan has actually started AND we have flushed the
        pre-scan camera backlog (see onNewScan). ``isScanRunning`` alone is
        insufficient: it turns True while the scan signals are still being
        armed, before any scan frame exists.
        """
        if self.__dict__.get('_shutdownComplete', False) or not self._scanArmed:
            return False
        if self._commChannel.isScanRunning():
            return True
        return self._drainingEndedScan and not self._hasExpectedScanFrames()

    def _expectedScanPixels(self) -> int | None:
        if self.dims is None:
            return None
        return int(self.dims[0]) * int(self.dims[1])

    def _workerFilledPixels(self) -> int:
        try:
            return int(self.beadWorker.filledPixels())
        except Exception:
            return 0

    def _receivedScanFrames(self) -> int:
        return max(int(self.framesReceivedThisScan), self._workerFilledPixels())

    def _hasExpectedScanFrames(self) -> bool:
        expected = self._expectedScanPixels()
        return expected is not None and self._receivedScanFrames() >= expected

    def _syncReconstructionFromWorker(self) -> None:
        try:
            buffer, filled_pixels = self.beadWorker.snapshot()
        except Exception:
            return
        if buffer is not None:
            self.recIm = buffer
        self.framesReceivedThisScan = max(
            int(self.framesReceivedThisScan),
            int(filled_pixels or 0),
        )

    def _discardBufferedFrames(self) -> bool:
        """Start BeadRec at an atomic post-boundary frame position.

        The detector drains its pre-boundary hardware chunk once and preserves
        it for consumers that were already registered (notably Recording).
        A global flush here would silently discard another owner's data.
        """
        with self._resourceLock():
            detectorName = self.__dict__.get('_scanDetectorName')
            generation = self.__dict__.get('_scanDetectorGeneration')
            handle = self.__dict__.get('_scanDetectorHandle')
        if detectorName is None or handle is None:
            return False
        self._master.detectorsManager.execOn(
            detectorName,
            lambda c: c.startChunkConsumer(self._CHUNK_CONSUMER),
        )
        with self._resourceLock():
            self._chunkConsumerDetectorName = detectorName
            self._chunkConsumerGeneration = generation
        return True

    def _cancelScanEndDrain(self) -> None:
        timer = self.__dict__.get('_scanEndDrainTimer')
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
            callback = self.__dict__.get('_scanEndDrainCallback')
            self._disconnectSignal(getattr(timer, 'timeout', None), callback)
        self._scanEndDrainCallback = None
        self._scanEndDrainGeneration = None
        self._drainingEndedScan = False
        self._scanEndDrainDeadline = None

    def _startScanEndDrain(self, generation: int) -> None:
        self._cancelScanEndDrain()
        timer = Timer(singleShot=True)
        callback = lambda: self._finishEndedScanIfReady(generation, timer)
        timer.timeout.connect(callback)
        self._scanEndDrainTimer = timer
        self._scanEndDrainCallback = callback
        self._scanEndDrainGeneration = generation
        self._drainingEndedScan = True
        self._scanEndDrainDeadline = (
            time.monotonic() + _SCAN_END_DRAIN_TIMEOUT_S
        )
        timer.start(_SCAN_END_DRAIN_CHECK_MS)

    def _getBeadRoiBounds(self) -> Sequence[int]:
        return self._widget.getROIGraphicsItem().bounds

    def _setResultRecords(self, records: Sequence[BeadRecResultRecord]) -> None:
        self.resultRecords = list(records)
        self.listRecs = [record.image for record in self.resultRecords]

    def _insertResultRecord(self, index: int, record: BeadRecResultRecord) -> None:
        self.resultRecords.insert(index, record)
        self.listRecs.insert(index, record.image)

    def _popResultRecord(self, index: int) -> None:
        self.resultRecords.pop(index)
        self.listRecs.pop(index)

    def _createAcquisitionConfig(self) -> BeadAcquisitionConfig:
        return BeadAcquisitionConfig.from_scan_dims(self.dims, frames_per_pixel=self.framesPerPixel)

    def _updateProgress(self, current: int, total: int) -> None:
        if self.__dict__.get('_shutdownComplete', False):
            return
        self._widget.updateProgress(current, total)

    def clearList(self):
        self._setResultRecords([])

    def selectionChanged(self,imgListIdx:int=None,currentRun=False,axialName=None):
        if currentRun:
            if axialName is None:
                axialName="XY"
            self.axialName = axialName
            self.imDisplay=self.currentRunImgs.get(axialName)
            self._displayPixelSizeUm = self._reconstructionPixelSizeUm(False)
        else:
            self.axialName = None
            if imgListIdx is not None and imgListIdx<len(self.resultRecords):
                self.imDisplay=self.resultRecords[imgListIdx].image
                self._displayPixelSizeUm = self.resultRecords[imgListIdx].pixel_size_um
            else:
                return
        
        if self.imDisplay is not None:
            self.updateScaling() # will update scaling and send final image to be displayed to widget
        else: 
            self._logger.warning(f"Selection changed, but self.imDisplay = none. current run: {currentRun} axialName: {axialName}")
        
    def removeRecFromList(self,idx:int=None):
        if idx is not None and idx<len(self.resultRecords):
            self._popResultRecord(idx)

    def addCurrentToWidgetList(self):
        axial=False
        axialName = None
        if self.autoAxial:
            if self._commChannel.getNextAxial() is not None:
                axialName = self._commChannel.getNextAxial()
                axial=True
        if not axial: # clean up currentRunImgs
            self.currentRunImgs={}

        self._widget.addCurrentRunToList(axial,axialName)
        self._widget.imageListWidget.setCurrentRow(0)


    def runFit(self, modelKey: str):
        if self.imDisplay is None:
            self._widget.setStatusText("No image to analyze")
            return
        
        if modelKey == "legacy_donut":
            result = analyze_donut(self.imDisplay, self._widget.analysisPrm)
            self._display_legacy_donut_result(result)
        else:
            from imswitch.imcontrol.model.bead_fits import FIT_MODELS
            try:
                result = fit_bead(self.imDisplay, modelKey, params=self._widget.analysisPrm)
            except ValueError as e:
                self._widget.setStatusText(f"Fit failed: {e}")
                return
            
            model = FIT_MODELS[modelKey]
            height, width = self.imDisplay.shape
            y_coords, x_coords = np.meshgrid(np.arange(height), np.arange(width), indexing='ij')
            # Fitted positional params are ROI-local; shift the evaluation
            # grid by the ROI origin to render on the full image.
            off_x, off_y = (result.roi[0], result.roi[1]) if result.roi else (0, 0)
            param_values = [result.params[name] for name in model.param_names]
            fit_image = model.model((x_coords - off_x, y_coords - off_y), *param_values)
            residual = self.imDisplay - fit_image
            
            metrics = {**result.params, "r_squared": result.r_squared, **result.summary}
            self._widget.displayFitResult(
                title=f"{modelKey} fit",
                metrics=metrics,
                image=self.imDisplay,
                overlay_center=result.center_px,
                fit_image=fit_image,
                residual=residual
            )
            self._widget.setStatusText(f"Fit complete: R²={result.r_squared:.4f}")
    
    def _display_legacy_donut_result(self, result):
        if not result.accepted:
            metrics = {"status": "rejected", "reason": result.reason or "Unknown"}
            if result.coord is not None:
                metrics["center_y"] = result.coord[0]
                metrics["center_x"] = result.coord[1]
            self._widget.displayFitResult(
                title="Legacy donut analysis (rejected)",
                metrics=metrics,
                image=self.imDisplay,
                overlay_center=result.coord
            )
            self._widget.setStatusText(f"Donut rejected: {result.reason}")
            return
        
        metrics = {
            "min_value": result.min_value,
            "background": result.background,
            "background_std": result.background_std,
            "fill_x": result.fill_x,
            "fill_x_std": result.fill_x_std,
            "fill_y": result.fill_y,
            "fill_y_std": result.fill_y_std,
            "center_y": result.coord[0],
            "center_x": result.coord[1],
        }
        self._widget.displayFitResult(
            title="Legacy donut analysis",
            metrics=metrics,
            image=self.imDisplay,
            overlay_center=result.coord
        )
        self._widget.setStatusText("Donut analysis complete")


    def loadImg(self):
        """Asks users to load one or several images, loads them to the list of saved images and
        calls widget function to add names of files to the list panel"""
        
        paths = guitools.askForFilePath(self._widget, 'Choose one or several tiff image(s)',defaultFolder=self.lastDir,
                                       isSaving=False,nameFilter= "TIFF Files (*.tif *.tiff)",multiFiles=True)
        if paths is None:
            return
        if isinstance(paths,list):
            self.lastDir = os.path.dirname(paths[0])
        else:
            paths = [paths]
        
        for path in paths:
            im = imread(path).astype(np.float64)
            if len(im.shape)!=2:
                self._logger.error("Loaded images should be 2d")
                return
            filename = os.path.splitext(os.path.basename(path))[0]
            itemName = self._widget.addToList(filename) # adds to list of items in widget
            # A file BeadRec saved carries its pixel size; keep it, so saving
            # it again does not drop the one fact a fit needs.
            pixelSize = read_pixel_size_um(path)
            self._insertResultRecord(
                0,
                BeadRecResultRecord(
                    name=itemName,
                    image=im,
                    source_path=path,
                    timestamp=time.time(),
                    pixel_size_um=pixelSize,
                ),
            )
        # display last image loaded
        self.imDisplay = im
        self._displayPixelSizeUm = pixelSize
        self._widget.updateImage(self.imDisplay)
        self._widget.imageListWidget.setCurrentRow(self._widget.getInsertIndexAfterCurrent())

    def addCurrentRun(self,name=None):
        """ Save current run to list of saved images, calls widget to add it
        to list of items and to delete the "current run" item(s), if a scan is not running. 
        NOTE: insert to first position to keep same order as widget items."""
        
        for key, img in self.currentRunImgs.items():
            scaled = False
            if self._widget.scaleButton.isChecked():
                img = self.rescale(img)
                scaled = True
            axialName = key if self.autoAxial else None
            itemName = self._widget.addToList(name,axialName)
            self._insertResultRecord(
                0,
                BeadRecResultRecord(
                    name=itemName,
                    image=img,
                    axial_name=axialName,
                    timestamp=time.time(),
                    scaled=scaled,
                    pixel_size_um=self._reconstructionPixelSizeUm(scaled),
                ),
            )
            if not self.ongoingScan:
                self._widget.removeCurrentRunItems()
                            

        # if not self.autoAxial and self.recIm is not None:
        #     self.update()
        #     self.listRecs.insert(0, self.imDisplay)
        #     self._widget.addToList(name)
        #     if not self.ongoingScan:
        #         self._widget.clearCurrentRunItem()
        # else:
        #     print("No current recon to add !")


    def saveRec(self):
        """ Saves currenlty display rec, so self.imDisplay. Suggests the filename if
        it can find name of selected row in the widget list panel"""
        if self.imDisplay is None:
            return

        #for filename suggestion
        if self._widget.isSelectedCurrent():
            suggested = self.lastDir
        else:
            idx = self._widget.imageListWidget.currentRow()
            if idx != -1:
                itemName = self._widget.imageListWidget.item(self._widget.imageListWidget.currentRow()).text()
                if self.lastDir is None:
                    suggested = itemName
                else:
                    suggested = os.path.join(self.lastDir,itemName)
            else:
                suggested = self.lastDir

        path = guitools.askForFilePath(self._widget, 'Save file as',defaultFolder=suggested,isSaving=True)
        if not path:
            return

        self.lastDir = os.path.dirname(path)
        if path.split('.')[-1] not in ['tif', 'tiff']:
            path = path + ".tiff"
        write_reconstruction_tiff(
            path, self.imDisplay, self.__dict__.get('_displayPixelSizeUm'),
            self._displayAnnotations(),
        )
    
    def saveAll(self):
        """ Saves all images that are in self.listRecs, with file names from the list panel."""
        if not self.resultRecords:
            return
        caption = "Choose folder to save all images"
        folder = guitools.askForFolderPath(self._widget, caption=caption, defaultFolder=self.lastDir)
        if not folder:
            return
        self.lastDir = os.path.dirname(folder)

        name_offset = self._widget.getInsertIndexAfterCurrent()
            
        for idx,record in enumerate(self.resultRecords):
            item = self._widget.imageListWidget.item(idx + name_offset)
            name = item.text() + ".tif"
            path = os.path.join(folder, name)
            write_reconstruction_tiff(
                path, record.image, record.pixel_size_um,
                self._recordAnnotations(record),
            )

    def roiToggled(self, enabled):
        """ Show or hide ROI."""
        if enabled:
            self.addROI()

            ROIsize = (64, 64)
            ROIcenter = self._commChannel.getCenterViewbox()

            ROIpos = (ROIcenter[0] - 0.5 * ROIsize[0],
                      ROIcenter[1] - 0.5 * ROIsize[1])

            self._widget.showROI(ROIpos, ROIsize)
            self._updateRoiCropPreview()
        else:
            self._widget.hideROI()
            # The preview replaced the viewer contents; restore the last
            # reconstruction (if any) when the ROI is hidden again.
            if self.recIm is not None and self.dims is not None:
                self.update()

    def _onLiveImage(self, detectorName, image, init, scale, isCurrentDetector):
        """Cache the current detector's latest frame for the ROI crop preview."""
        if self.__dict__.get('_shutdownComplete', False) or not isCurrentDetector:
            return
        self._lastLiveFrame = np.asarray(image)
        self._updateRoiCropPreview()

    def _onRoiChanged(self, position=None, size=None):
        if self.__dict__.get('_shutdownComplete', False):
            return
        self._updateRoiCropPreview()

    def _updateRoiCropPreview(self):
        """Show exactly what the ROI crops from the live current-detector frame.

        Active only while the ROI is shown and BeadRec is NOT reconstructing
        (during a run the reconstruction owns the viewer). The crop is taken
        from the same raw frame and ROI bounds the reconstruction uses, so it
        is a faithful preview of what each pixel will be averaged from.
        """
        if self.running or not self._widget.roiButton.isChecked():
            return
        frame = self._lastLiveFrame
        if frame is None or frame.ndim != 2 or frame.size == 0:
            return
        # Crop the displayed (transformed) frame so the preview matches what the
        # ROI overlays on screen and what the reconstruction will average.
        frame = self._toDisplayedFrames([frame])[0]
        try:
            roi = normalize_roi_bounds(self._getBeadRoiBounds(), frame.shape)
        except ValueError:
            return
        rows, cols = roi.as_slices()
        crop = frame[rows, cols]
        if crop.size == 0:
            return
        # A live crop, not an orientable reconstruction: drop the orient base so
        # rotate/flip toggles don't redraw a stale reconstruction over it.
        self._orientBase = None
        self.imDisplay = crop
        self._widget.updateImage(crop, autoLevels=True)

    def addROI(self):
        """ Adds the ROI to ImageWidget viewbox through the CommunicationChannel. """
        if not self.roiAdded:
            self._commChannel.sigAddItemToVb.emit(self._widget.getROIGraphicsItem())
            self.roiAdded = True

    def run(self):
        if self.__dict__.get('_shutdownComplete', False):
            return
        if self._widget.runButton.isChecked():
            with self._resourceLock():
                workerStopping = self.__dict__.get(
                    '_beadWorkerStopping', False
                )
            if workerStopping or self._workerThreadIsRunning(self.thread):
                self._rejectRunStart(
                    'Bead reconstruction is still stopping; wait for it to '
                    'finish before starting again'
                )
                return

            self._cancelScanEndDrain()
            self._scanArmed = False
            try:
                self.updateParameters()
                config = self._createAcquisitionConfig()
                scanAlreadyRunning = self._commChannel.isScanRunning()
                if scanAlreadyRunning:
                    # The normal signal already passed, so take the same
                    # detector ownership explicitly before allowing reads.
                    self.onScanStarting()
                    if not self._hasScanDetectorLease():
                        self._rejectRunStart(
                            'Bead reconstruction could not arm the active '
                            'scan detector'
                        )
                        return
                    if not self._discardBufferedFrames():
                        self._rejectRunStart(
                            'Bead reconstruction has no armed scan detector'
                        )
                        return
                    self._scanArmed = True

                self.beadWorker.start(config)
                with self._resourceLock():
                    generation = (
                        self.__dict__.get('_beadWorkerGeneration', 0) + 1
                    )
                    self._beadWorkerGeneration = generation
                    self._activeBeadWorkerGeneration = generation
                    self._beadWorkerStopping = False
                self.thread.start()
            except Exception as e:
                try:
                    self.beadWorker.stop()
                except Exception:
                    pass
                with self._resourceLock():
                    self._activeBeadWorkerGeneration = None
                    self._beadWorkerStopping = False
                self._scanArmed = False
                self._releaseBeadDetectorResources()
                self._logger.error(
                    f'Bead reconstruction could not start: {e}',
                    exc_info=True,
                )
                self._rejectRunStart(
                    f'Bead reconstruction could not start: {e}'
                )
                return

            self.running = True
            self._widget.setStatusText("Bead reconstruction running")
            self._widget.updateProgress(0, config.total_pixels)
            if self.ongoingScan:
                self.addCurrentToWidgetList()
        else:
            self.running = False
            self._scanArmed = False
            self._cancelScanEndDrain()
            workerStopped = False
            with self._resourceLock():
                self._scanGeneration = (
                    self.__dict__.get('_scanGeneration', 0) + 1
                )
                activeWorkerGeneration = self.__dict__.get(
                    '_activeBeadWorkerGeneration'
                )
                if activeWorkerGeneration is not None:
                    self._beadWorkerStopping = True
            try:
                self.beadWorker.stop()
            except Exception:
                pass
            try:
                self._widget.setStatusText("Bead reconstruction stopped")
            except Exception:
                pass
            try:
                self.thread.quit()
                workerStopped = self._waitForWorkerThread(self.thread)
            finally:
                # Run-off is a terminal path for the current reconstruction. A
                # pre-arm acquired by sigScanStarting must not survive if the
                # user toggles Run off before sigScanEnded arrives. If the
                # worker is still inside a hardware read, its DirectConnection
                # finished callback performs this cleanup later.
                if workerStopped:
                    self._onBeadWorkerThreadFinished(
                        activeWorkerGeneration
                    )
                    if activeWorkerGeneration is None:
                        self._releaseBeadDetectorResources()

    def _rejectRunStart(self, message: str) -> None:
        self.running = False
        self._scanArmed = False
        try:
            self._widget.runButton.setChecked(False)
        except Exception:
            pass
        try:
            self._widget.setStatusText(message)
        except Exception:
            pass
        self._logger.warning(message)

    def onNewScan(self):
        if self.__dict__.get('_shutdownComplete', False):
            return
        self.newScan = True
        self.framesReceivedThisScan = 0
        self._cancelScanEndDrain()
        if self.autoAxial:
            self.axialName = self._commChannel.getNextAxial()
        else:
            self.axialName = "XY"

        if self._widget.runButton.isChecked():
            if not self._hasScanDetectorLease():
                self._scanArmed = False
                message = (
                    'BeadRec was not pre-armed for this scan; reconstruction '
                    'is disabled to avoid an unowned detector read'
                )
                self._logger.error(message)
                self._widget.setStatusText(message)
                return
            # Establish BeadRec's own post-start consumer boundary without
            # flushing frames retained for Recording, then reset/configure the
            # reconstruction buffer.
            try:
                if not self._discardBufferedFrames():
                    raise RuntimeError('no armed detector is available')
                self.beadWorker.configure(self._createAcquisitionConfig())
            except Exception as e:
                self._scanArmed = False
                self._logger.error(
                    f'BeadRec could not prepare its armed detector: {e}',
                    exc_info=True,
                )
                self._widget.setStatusText(
                    f'BeadRec could not prepare its armed detector: {e}'
                )
                self._releaseBeadDetectorResources()
                return
            self._scanArmed = True
            self.addCurrentToWidgetList() # in case "clear all" made it disappear
            # BeadRec reconstructs from the CURRENT detector (execOnCurrent);
            # surface which one that is, since picking the wrong camera in
            # the view silently yields an empty/garbage reconstruction.
            try:
                detectorName = self._reconstructionDetectorName()
                self._logger.info(
                    f'BeadRec scan started: reconstructing from pinned '
                    f'detector "{detectorName}"'
                )
                self._widget.setStatusText(
                    f'Reconstructing from "{detectorName}"'
                )
            except Exception:
                pass

    
    def OngoingScanStatus(self):
        if self.__dict__.get('_shutdownComplete', False):
            return
        self.ongoingScan = True

    def onEndedScan(self):
        if self.__dict__.get('_shutdownComplete', False):
            return
        self.ongoingScan=False
        # If self.framesReceivedThisScan == 0 after the short drain below,
        # _completeEndedScan reports "0 detector frames received" loudly.
        should_drain = (
            self.running
            and self._expectedScanPixels() is not None
            and not self._hasExpectedScanFrames()
        )
        if should_drain:
            # Live-view-only mock scans can emit sigScanEnded before the final
            # detector frames have been delivered to the chunk buffer. The
            # RecordingManager avoids this by polling until its expected frame
            # count is reached; mirror that behavior briefly for BeadRec.
            self._scanArmed = True
            with self._resourceLock():
                generation = self.__dict__.get('_scanDetectorGeneration')
            if generation is None:
                self._scanArmed = False
                self._completeEndedScan()
                return
            self._startScanEndDrain(generation)
            return
        self._completeEndedScan()

    def _finishEndedScanIfReady(
        self, generation=None, sourceTimer=None
    ) -> None:
        if self.__dict__.get('_shutdownComplete', False):
            return
        if (
            not self.__dict__.get('_drainingEndedScan', False)
            or generation != self.__dict__.get('_scanEndDrainGeneration')
            or (
                sourceTimer is not None
                and sourceTimer is not self.__dict__.get(
                    '_scanEndDrainTimer'
                )
            )
        ):
            return
        deadline = self._scanEndDrainDeadline
        if (
            self._hasExpectedScanFrames()
            or deadline is None
            or time.monotonic() >= deadline
        ):
            self._completeEndedScan(generation)
        else:
            self._scanEndDrainTimer.start(_SCAN_END_DRAIN_CHECK_MS)

    def _completeEndedScan(self, expectedGeneration=None) -> None:
        if self.__dict__.get('_shutdownComplete', False):
            return
        with self._resourceLock():
            generation = self.__dict__.get('_scanDetectorGeneration')
        if (
            expectedGeneration is not None
            and generation is not None
            and generation != expectedGeneration
        ):
            return
        self._cancelScanEndDrain()
        self._syncReconstructionFromWorker()
        # Stop consuming: frames the free-running camera keeps producing after
        # the scan ends must not bleed into the finished reconstruction.
        self._scanArmed = False
        self._releaseBeadDetectorResources(generation)
        received = self._receivedScanFrames()
        if self.running and received == 0:
            msg = ('BeadRec: 0 detector frames received during the scan — '
                   'check camera triggering (e.g. external-trigger TTL '
                   'never pulsed).')
            self._logger.warning(msg)
            self._widget.setStatusText(msg)
            return
        if self.recIm is None:
            self._widget.setStatusText("Scan ended without bead reconstruction data")
            return
        # Diagnostic: kept frames vs expected pixels. A surplus means frames
        # leaked in (e.g. pre-scan buffer not flushed) or the camera produced
        # more triggers than the grid expects; a deficit means dropped triggers.
        if self.dims is not None:
            expected = self.dims[0] * self.dims[1]
            if received != expected:
                self._logger.warning(
                    'BeadRec frame-count mismatch: received %d kept frames, '
                    'expected %d (%dx%d). Surplus -> pre-scan/extra frames; '
                    'deficit -> dropped triggers.',
                    received, expected, self.dims[0], self.dims[1]
                )
            else:
                self._logger.info(
                    'BeadRec received %d frames, matching the expected %dx%d grid.',
                    received, self.dims[0], self.dims[1]
                )
        self.currentRunImgs[self.axialName] = reconstruction_image(self.recIm, self.dims) # we always store unscaled img
        self._widget.setStatusText("Bead reconstruction scan complete")
        self._widget.updateProgress(self.recIm.size, self.recIm.size)
    
    def onAutoAxialToggled(self,state:bool = False):
        if self.__dict__.get('_shutdownComplete', False):
            return
        if state:
            self.autoAxial=True
        else:
            self.autoAxial=False
    
    def onNewAxialListBuffer(self, axialList:list):
        """ clean up self.currentRunImgs to not keep previous XZ/YZ and widget list """
        if self.__dict__.get('_shutdownComplete', False):
            return
        self.currentRunImgs={}
        self._widget.removeCurrentRunItems()

    def updateParameters(self):
        if self.__dict__.get('_shutdownComplete', False):
            return
        try:
            dims = np.array(self._commChannel.getDimsScan()).astype(int)
            stepSizes = np.array(self._commChannel.getScanStepSizes(), dtype=float)
        except RuntimeError:
            # Neither a generic 'Scan' controller nor a BeadRec-compatible
            # scan source (e.g. TriggerScope raster) is registered in this
            # setup. Bead reconstruction needs scan dimensions it cannot
            # obtain here, so skip instead of raising on every scan start.
            # Warn only once.
            if not self._warnedNoScanWidget:
                self._logger.warning(
                    'Bead reconstruction inactive: no scan widget or BeadRec '
                    'scan source available to provide scan dimensions '
                    '(getDimsScan). Skipping parameter update on scan start.'
                )
                self._warnedNoScanWidget = True
            return

        # Use explicit axes 0 and 1 (X, Y) from getDimsScan
        if dims[0] <= 0 or dims[1] <= 0:
            self._logger.warning(
                f'BeadRec: invalid scan dimensions ({dims[0]}, {dims[1]}). '
                f'Keeping previous dims.'
            )
            self.parametersChanged = False
            return

        prior_dims = self.dims
        prior_stepSizes = self.stepSizes
        prior_framesPerPixel = self.framesPerPixel

        self.dims = (int(dims[0]), int(dims[1]))
        self.stepSizes = (float(stepSizes[0]), float(stepSizes[1]))
        self.framesPerPixel = self._commChannel.getFramesPerScanPixel()
        
        if prior_dims is not None and prior_stepSizes is not None:
            if prior_dims != self.dims or prior_stepSizes != self.stepSizes or prior_framesPerPixel != self.framesPerPixel:
                self.parametersChanged = True
    
    def updateOnMousePixelValue(self,x,y):
        """ Updates the pixel value displayed in the widget """
        if self.imDisplay is not None:
            if 0 <= x < self.imDisplay.shape[1] and 0 <= y < self.imDisplay.shape[0]:
                val = self.imDisplay[round(y), round(x)]
                self._widget.updatePixelValue(x,y,val)
            else:
                self._widget.erasePixelValue()

    def _applyOrientation(self, im):
        """Apply the reconstruction orientation (rotate/flip) for display."""
        if im is None:
            return None
        oriented, _ = apply_display_transform(im, None, self._orientation)
        return oriented

    def _showReconstruction(self, base, pixelSizeUm=None):
        """Orient `base` and show it, remembering it for re-orientation.

        `base` is the reconstruction after optional physical-pixel rescaling but
        before orientation. self.imDisplay holds the oriented image actually
        shown (so fits, saves and the pixel readout all use what the user sees).
        `pixelSizeUm` is `base`'s; the shown image's follows the rotation.
        """
        self._orientBase = base
        self._orientBasePixelSizeUm = pixelSizeUm
        self.imDisplay = self._applyOrientation(base)
        self._displayPixelSizeUm = oriented_pixel_size(
            pixelSizeUm, self._orientation.rotation
        )
        self._widget.updateImage(self.imDisplay)

    def _onOrientationChanged(self):
        """Re-render the current reconstruction with the new orientation."""
        if self.__dict__.get('_shutdownComplete', False):
            return
        self._orientation = DisplayTransform(*self._toTransformArgs())
        if self._orientBase is not None:
            self.imDisplay = self._applyOrientation(self._orientBase)
            self._displayPixelSizeUm = oriented_pixel_size(
                self.__dict__.get('_orientBasePixelSizeUm'),
                self._orientation.rotation,
            )
            self._widget.updateImage(self.imDisplay)

    def _toTransformArgs(self):
        rotation, flipH, flipV = self._widget.getOrientation()
        return rotation, bool(flipH), bool(flipV)

    def updateScaling(self):
        """ Updates scaling factor of displayed image, only if current run
        Note that this will overwrite imDisplay with scaled version, but unscaled still accessble with currentRunImgs[self.axialName]"""
        if not self._commChannel.isScanRunning() and self._widget.isSelectedCurrent():
            # Rescale from the raw stored reconstruction (not the already-shown
            # image) so scale + orientation never compound across toggles.
            base = self.currentRunImgs.get(self.axialName)
            scaled = base is not None and self._widget.scaleButton.isChecked()
            if scaled:
                base = self.rescale(base)
            if base is not None:
                self._showReconstruction(
                    base, self._reconstructionPixelSizeUm(scaled)
                )
                return
        # Showing a saved list item (not the live reconstruction): orientation
        # toggles must not resurrect a stale reconstruction.
        self._orientBase = None
        self._widget.updateImage(self.imDisplay)

    def _reconstructionPixelSizeUm(self, scaled):
        """``(y, x)`` in µm of the current reconstruction, before orientation.

        One scan step per pixel; after rescaling, the finer of the two steps on
        both axes (``rescale_reconstruction_to_pixel_size`` resamples to it).
        None when the scan steps are unknown.
        """
        steps = self.__dict__.get('stepSizes')
        if steps is None or len(steps) < 2:
            return None
        pixel = valid_pixel_size((steps[1], steps[0]))    # stepSizes is (x, y)
        if pixel is None:
            return None
        if scaled and not np.isclose(pixel[0], pixel[1]):
            finer = min(pixel)
            return (finer, finer)
        return pixel

    def _displayAnnotations(self):
        """What the file of the image on screen should say about it."""
        rotation, flipH, flipV = self._toTransformArgs()
        annotations = {
            'BeadRec:axial_name': self.__dict__.get('axialName'),
            'BeadRec:orientation': {
                'rotation': int(rotation), 'flip_h': bool(flipH),
                'flip_v': bool(flipV),
            },
        }
        if self.__dict__.get('dims') is not None:
            annotations['BeadRec:scan_dims'] = [int(v) for v in self.dims]
        if self.__dict__.get('stepSizes') is not None:
            annotations['BeadRec:scan_step_um'] = [float(v) for v in self.stepSizes]
        annotations['BeadRec:frames_per_pixel'] = int(
            self.__dict__.get('framesPerPixel') or 1
        )
        return annotations

    @staticmethod
    def _recordAnnotations(record):
        """A saved record's own metadata, as OME key/value pairs."""
        return {
            f'BeadRec:{key}': value
            for key, value in record.metadata().items()
            if key != 'pixel_size_um' and value is not None
        }

    def rescale(self,im):
        """
        Rescale image to physical scan pixel size if x/y step sizes differ.
        """
        try:
            return rescale_reconstruction_to_pixel_size(im, self.stepSizes)
        except ValueError as exc:
            self._logger.warning("Could not rescale BeadRec image: %s", exc)
            return im
    
    def update(self, recIm=None):
        """"Updates image display with current recorded image self.recIm"""
        if self.__dict__.get('_shutdownComplete', False):
            return
        if isinstance(recIm, BeadWorkerUpdate):
            self._updateProgress(recIm.filled_pixels, recIm.total_pixels)
            self.framesReceivedThisScan += recIm.frames_written
            recIm = recIm.buffer
        if recIm is not None:
            self.recIm = recIm
        if self.recIm is None:
            return
        base = reconstruction_image(self.recIm, self.dims)
        scaled = bool(self._widget.scaleButton.isChecked())
        if scaled:
            base = self.rescale(base)
        self._showReconstruction(base, self._reconstructionPixelSizeUm(scaled))


    def centerCoordQuery(self, mode):
        if self.__dict__.get('_shutdownComplete', False):
            return
        coord = None
        if self.imDisplay is not None:
            model_key = "gaussian2d" if mode == "Maxima" else "donut_r2_gaussian"
            try:
                result = fit_bead(self.imDisplay, model_key, params=self._widget.analysisPrm)
                coord = (round(result.center_px[0]), round(result.center_px[1]))
            except ValueError:
                result = find_bead_center(self.imDisplay, mode, self._widget.analysisPrm)
                coord = result.coord
        
        self._commChannel.beadRecWorkflow.finish_center_coord_pipeline(coord)
        if coord is not None and self.showCenterState:
            self._widget.displayCenterCoord(coord[0], coord[1])
        else:
            self._logger.warning(f"Center search with '{mode}' method failed. Try manual coordinate")

    def updateCenterCross(self,y,x):    
        if self.__dict__.get('_shutdownComplete', False):
            return
        self.yCenter = y
        self.xCenter = x
        self.updateCenterCrossWidget()
    
    def showStateChanged(self,state:bool):
        if self.__dict__.get('_shutdownComplete', False):
            return
        self.showCenterState = state
        self.updateCenterCrossWidget()

    def updateCenterCrossWidget(self):
        if self.showCenterState and self.imDisplay is not None and self.yCenter is not None and self.xCenter is not None:   
            self._widget.displayCenterCoord(self.yCenter,self.xCenter)
        else:
            self._widget.removeCenterCoord()

    # Unified State Persistence Interface (StatefulComponentMixin)
    
    def getComponentState(self) -> dict:
        """Snapshot current BeadRec analysis settings and UI state.
        
        Returns passive BeadRec UI state for persistence.
        Does NOT include reconstruction buffers or ongoing acquisition state.
        
        Returns:
            {
                'analysis_parameters': dict,
                'scale_enabled': bool,
                'roi_visible': bool,
                'orientation': {'rotation': int, 'flipH': bool, 'flipV': bool},
                'last_dir': str | None,
                'result_metadata': list
            }
        """
        return {
            "analysis_parameters": BeadAnalysisParameters.from_mapping(
                self._widget.analysisPrm
            ).as_dict(),
            "scale_enabled": self._widget.scaleButton.isChecked(),
            "roi_visible": self._widget.roiButton.isChecked(),
            "orientation": dict(zip(("rotation", "flipH", "flipV"),
                                    self._widget.getOrientation())),
            "last_dir": self.lastDir,
            "result_metadata": [
                record.metadata() for record in self.resultRecords
            ],
        }
    
    def applyComponentState(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode
    ) -> list[str]:
        """Restore BeadRec analysis settings and UI state from a snapshot.
        
        IDENTICAL behavior in both STARTUP_RESTORE and SETUP_MODE_APPLY:
        - Restore analysis parameters
        - Restore UI settings (scale, ROI visibility, orientation)
        - Restore last directory if it exists
        
        NEVER (in either mode):
        - Start reconstruction
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
        
        try:
            analysisParameters = state.get("analysis_parameters")
            if isinstance(analysisParameters, dict):
                self._widget.analysisPrm = BeadAnalysisParameters.from_mapping(
                    analysisParameters
                ).as_dict()
        except Exception as exc:
            warnings.append(f"Failed to restore analysis parameters: {exc}")
        
        try:
            self._widget.scaleButton.setChecked(bool(state.get("scale_enabled", False)))
        except Exception as exc:
            warnings.append(f"Failed to restore scale setting: {exc}")
        
        try:
            orientation = state.get("orientation")
            if isinstance(orientation, dict):
                self._widget.setOrientation(
                    orientation.get("rotation", 0),
                    orientation.get("flipH", False),
                    orientation.get("flipV", False),
                )
                self._orientation = DisplayTransform(*self._toTransformArgs())
        except Exception as exc:
            warnings.append(f"Failed to restore orientation: {exc}")
        
        lastDir = state.get("last_dir")
        if isinstance(lastDir, str):
            if os.path.isdir(lastDir):
                self.lastDir = lastDir
            else:
                warnings.append(f'Last directory "{lastDir}" does not exist; skipped.')
        
        try:
            roiVisible = bool(state.get("roi_visible", False))
            signalsBlocked = self._widget.roiButton.blockSignals(True)
            self._widget.roiButton.setChecked(roiVisible)
            self._widget.roiButton.blockSignals(signalsBlocked)
            self.roiToggled(roiVisible)
        except Exception as exc:
            warnings.append(f"Failed to restore ROI visibility: {exc}")
        
        return warnings
    
    def describeComponentState(self, state: dict) -> list[str]:
        """Generate human-readable summary of saved BeadRec settings.
        
        Args:
            state: Dict returned by getComponentState()
        
        Returns:
            List of formatted strings suitable for setup-mode inspector
        """
        summaries = []
        
        analysisParams = state.get("analysis_parameters", {})
        if analysisParams:
            summaries.append('  analysis parameters:')
            for key, value in sorted(analysisParams.items()):
                summaries.append(f'    {key}: {value}')
        
        scaleEnabled = state.get("scale_enabled", False)
        summaries.append(f'  scale enabled: {scaleEnabled}')
        
        roiVisible = state.get("roi_visible", False)
        summaries.append(f'  ROI visible: {roiVisible}')
        
        orientation = state.get("orientation", {})
        if orientation:
            rot = orientation.get("rotation", 0)
            flipH = orientation.get("flipH", False)
            flipV = orientation.get("flipV", False)
            summaries.append(f'  orientation: rotation={rot}°, flipH={flipH}, flipV={flipV}')
        
        lastDir = state.get("last_dir")
        if lastDir:
            summaries.append(f'  last directory: {lastDir}')
        
        return summaries or ['  no BeadRec settings saved']
    
    def getComponentStateHazards(
        self,
        state: dict,
        *,
        applyMode: ComponentStateApplyMode,
        context: dict | None = None
    ) -> list[dict]:
        """Identify potential hazards in saved BeadRec state.
        
        BeadRec analysis settings have no hazards (they do not start acquisition).
        
        Args:
            state: Dict returned by getComponentState()
            applyMode: STARTUP_RESTORE or SETUP_MODE_APPLY
            context: Optional consumer-provided context (unused)
        
        Returns:
            Empty list (no hazards)
        """
        return []
        

            

class BeadWorker(Worker):
    sigNewChunk = Signal(object)
    sigWarning = Signal(str)
    sigProgress = Signal(int, int)

    def __init__(
        self,
        isScanRunning: Callable[[], bool],
        getFrames: Callable[[], Sequence[np.ndarray]],
        getRoiBounds: Callable[[], Sequence[int]],
    ) -> None:
        super().__init__()
        self._isScanRunning = isScanRunning
        self._getFrames = getFrames
        self._getRoiBounds = getRoiBounds
        self._lock = Lock()
        self._running = False
        self._config = None
        self._recIm = None
        self._nextIndex = 0
        self._filledPixels = 0
        self._resetRequested = False
        self._lineStepPhase = 0

    def start(self, config: BeadAcquisitionConfig) -> None:
        self.configure(config)
        with self._lock:
            self._running = True

    def stop(self) -> None:
        with self._lock:
            self._running = False

    def configure(self, config: BeadAcquisitionConfig) -> None:
        with self._lock:
            self._config = config
            self._resetRequested = True
            self._lineStepPhase = 0
            self._filledPixels = 0
            self._nextIndex = 0

    def _isRunning(self) -> bool:
        with self._lock:
            return self._running

    def filledPixels(self) -> int:
        with self._lock:
            return int(self._filledPixels)

    def snapshot(self) -> tuple[np.ndarray | None, int]:
        with self._lock:
            return self._recIm, int(self._filledPixels)

    def _getBufferAndIndex(
        self,
    ) -> tuple[BeadAcquisitionConfig | None, np.ndarray | None, int | None]:
        with self._lock:
            if self._config is None:
                return None, None, None
            if self._recIm is None or self._resetRequested:
                self._recIm = create_reconstruction_buffer(self._config.scan_dims)
                self._nextIndex = 0
                self._filledPixels = 0
                self._resetRequested = False
            return self._config, self._recIm, self._nextIndex
    
    def _storeUpdate(
        self,
        sourceBuffer: np.ndarray,
        update: ReconstructionUpdate,
    ) -> BeadWorkerUpdate | None:
        with self._lock:
            if self._recIm is not sourceBuffer:
                return None
            self._recIm = update.buffer
            self._nextIndex = update.next_index
            if update.wrapped:
                self._filledPixels = self._recIm.size
            else:
                self._filledPixels = min(self._filledPixels + update.frames_written, self._recIm.size)
            return BeadWorkerUpdate(
                buffer=self._recIm,
                filled_pixels=self._filledPixels,
                total_pixels=self._recIm.size,
                frames_written=update.frames_written,
                wrapped=update.wrapped,
            )

    def run(self) -> None:
        while self._isRunning():
            config, recIm, nextIndex = self._getBufferAndIndex()
            if config is None or recIm is None or nextIndex is None:
                time.sleep(0.0001)
                continue

            if self._isScanRunning():
                newImages = self._getFrames()
                n = len(newImages)
                if n > 0:
                    # With C = frames_per_pixel > 1 the camera fires in C
                    # linesteps per line, so frames arrive in per-line blocks
                    # of Nx per linestep: [line0 step0: Nx][line0 step1: Nx]...
                    # Keep only the first block (linestep) of each physical
                    # line: frames whose index modulo Nx*C falls in [0, Nx).
                    C = config.frames_per_pixel
                    if C > 1:
                        Nx = config.scan_dims[0]
                        period = Nx * C
                        keptFrames = [
                            newImages[i]
                            for i in range(n)
                            if (self._lineStepPhase + i) % period < Nx
                        ]
                        self._lineStepPhase = (self._lineStepPhase + n) % period
                    else:
                        keptFrames = newImages
                    
                    if len(keptFrames) > 0:
                        try:
                            roi = normalize_roi_bounds(self._getRoiBounds(), keptFrames[0].shape)
                            update = append_roi_means(
                                recIm,
                                nextIndex,
                                keptFrames,
                                roi,
                                wrap=config.wrap,
                            )
                        except ValueError as exc:
                            self.sigWarning.emit(f"Skipping BeadRec chunk: {exc}")
                            continue

                        workerUpdate = self._storeUpdate(recIm, update)
                        if workerUpdate is not None:
                            self.sigNewChunk.emit(workerUpdate)
                            self.sigProgress.emit(workerUpdate.filled_pixels, workerUpdate.total_pixels)

            time.sleep(config.poll_interval_s)  # Prevents freezing











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
