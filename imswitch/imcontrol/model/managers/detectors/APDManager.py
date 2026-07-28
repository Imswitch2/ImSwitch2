import numpy as np
import matplotlib.pyplot as plt
import threading
import time

from imswitch.imcommon.framework import Signal, Thread, Worker
from imswitch.imcommon.model import initLogger
from .._scan_execution import PARTICIPANTS_KEY
from .DetectorManager import DetectorManager
from ._live_display import LiveDisplayThrottle

UpdateRateInPixels = 0.05 # update image every Xth pixel, depends on how efficient the data transfer code is.
_SCAN_THREAD_JOIN_TIMEOUT_MS = 2000


def _joinScanThreadBounded(thread, timeoutMs, detectorName):
    """Join framework.Thread without relying on an unavailable timeout API."""
    timeoutMs = max(0, int(timeoutMs))
    try:
        joined = thread.wait(timeoutMs)
    except TypeError:
        deadline = time.monotonic() + timeoutMs / 1000
        while thread.isRunning():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f'{detectorName} scan thread did not stop within '
                    f'{timeoutMs / 1000:g} s'
                )
            time.sleep(min(0.01, remaining))
        # The abstraction's wait() has no timeout. Calling it only after
        # isRunning() is false makes this a non-blocking reap.
        thread.wait()
        return

    if joined is False or thread.isRunning():
        raise RuntimeError(
            f'{detectorName} scan thread did not stop within '
            f'{timeoutMs / 1000:g} s'
        )


class APDManager(DetectorManager):
    """ DetectorManager that deals with an avalanche photodiode connected to a
    counter input on a Nidaq card.

    Manager properties:
    - ``terminal`` -- the physical input terminal on the Nidaq to which the APD
      is connected
    - ``ctrInputLine`` -- the counter that the physical input terminal is
      connected to
    """

    def __init__(self, detectorInfo, name, nidaqManager, **_lowLevelManagers):

        self.__logger = initLogger(self, instanceName=name)

        model = name
        self._name = name
        self.setPixelSize([1, 1])
        fullShape = (100, 100)
        # APD photon counts are non-negative integers — uint16 is sufficient
        # for typical fluorescence; the TTL-multiplying path will reallocate
        # as float32 (to preserve NaN as a "no-data" marker) in initiateImage.
        self._image = np.zeros(fullShape, dtype=np.uint16)
        self._detection_samplerate = float(1e6)
        self._nidaq_clock_source = r'ctr2InternalOutput'  # counter output task generating a 1 MHz frequency digitial pulse train
        manager_props = detectorInfo.managerProperties
        self._channel = manager_props["ctrInputLine"]
        device_name = manager_props.get("deviceName", "Dev1")
        if isinstance(self._channel, int):
            self._channel = f'{device_name}/ctr{self._channel}'  # for backwards compatibility
        self._terminal = manager_props["terminal"]
        self._mock_photon_count_mean = float(
            manager_props.get(
                "mockPhotonCountMean",
                manager_props.get("mock_photon_count_mean", 800.0),
            )
        )
        self._mock_photon_count_max = max(
            1,
            int(
                manager_props.get(
                    "mockPhotonCountMax",
                    manager_props.get("mock_photon_count_max", 5000),
                )
            ),
        )
        self._mock_random_seed = manager_props.get(
            "mockRandomSeed",
            manager_props.get("mock_random_seed", None),
        )
        self._warned_mock_count_clip = False

        self._frameCount = 0
        self._scanWorker = None
        self._scanThread = None
        self._scanParticipating = False
        self._scanLifecycleLock = threading.Lock()
        self._scanGeneration = 0
        self._preparedScanGeneration = None
        self._activeScanGeneration = None
        self._tearingDownScanGenerations = set()
        self._completedScanGenerations = set()
        self._finishAcks = {}
        self._scanTeardownOperation = None
        self.__newFrameReady = False
        self._ttlmultiplying = False
        self.acquisition = True
        # Deterministic rate limiter for the in-progress live preview. Caps
        # full-image napari redraws at a fixed rate (default 20 Hz) instead of
        # the old shape-dependent random gate that flooded the GUI on fast
        # scans. Tunable per rig via the ``liveUpdateIntervalMs`` config prop.
        self._liveThrottle = LiveDisplayThrottle(
            min_interval_s=max(
                0.0,
                float(manager_props.get("liveUpdateIntervalMs", 50.0)) / 1000.0,
            )
        )
        self._debug_mode = False  # run mode for plotting detected samples
        # Generate detected samples instead of reading the NI-DAQ counter input.
        # Forced on whenever the NI-DAQ itself is simulating: with no hardware
        # the counter-input task is None, so reading it would crash
        # (startInputTask -> None.start()). An explicit config flag can also turn
        # it on against a real NI-DAQ for bench testing.
        self._simulation_mode = bool(
            manager_props.get("simulation_mode", False)
            or getattr(nidaqManager, 'isSimulated', False)
        )

        # Prepare detector manager parameters and signal connections
        parameters = {}
        self._nidaqManager = nidaqManager
        self._nidaqManager.sigScanBuilt.connect(
            self._onScanBuilt
        )
        self._nidaqManager.sigScanStarted.connect(self.startScan)
        self.__shape = fullShape
        super().__init__(detectorInfo, name, fullShape=fullShape, supportedBinnings=[1],
                         model=model, parameters=parameters, croppable=False)

    def __del__(self):
        try:
            self._teardownScan(raiseErrors=False)
        except Exception as error:
            try:
                state = object.__getattribute__(self, '__dict__')
            except Exception:
                state = {}
            logger = state.get('_APDManager__logger')
            if logger is not None:
                logger.warning(
                    'Failed to clean up APD scan worker: %s', error
                )
        if hasattr(super(), '__del__'):
            super().__del__()

    def initiateScan(self, scanInfoDict, signalDict):
        participants = scanInfoDict.get(PARTICIPANTS_KEY)
        self._scanParticipating = (
            participants is None or self.name in participants
        )
        self._preparedScanGeneration = None
        if not self._scanParticipating:
            return

        # The scan-scoped participant snapshot is authoritative.  Do not gate
        # on ``acquisition``: it is a coarse hardware-state flag and older
        # versions left it true after the last lease was released.
        # Fresh scan: let the first line refresh the preview immediately.
        with self._scanLifecycleLock:
            operation = self._scanTeardownOperation
            needsTeardown = (
                self._scanWorker is not None
                or self._scanThread is not None
                or (
                    operation is not None
                    and not operation['event'].is_set()
                )
            )
        if needsTeardown:
            self._teardownScan(raiseErrors=True)
        with self._scanLifecycleLock:
            self._scanGeneration += 1
            generation = self._scanGeneration
            self._completedScanGenerations = {
                item for item in self._completedScanGenerations
                if item >= generation - 8
            }
        self._liveThrottle.reset()
        self._scanWorker = ScanWorker(self, scanInfoDict, signalDict)
        self._scanWorker.scanGeneration = generation
        self._scanTeardownOperation = None
        self._scanThread = Thread()
        self._scanWorker.moveToThread(self._scanThread)
        self._scanThread.started.connect(self._scanWorker.run)
        self._scanWorker.scanning = True
        self._scanWorker.d2Step.connect(
            self._onScanPixels
        )
        self._scanWorker.acqDoneSignal.connect(self.stopAcquisitionLocal)
        self._scanWorker.d3Step.connect(self._onFrameBoundary)
        if self._debug_mode:
            plt.figure(1)
        self._linestep = getattr(self._scanWorker, "_linestep", 1)
        self._preparedScanGeneration = generation

    def _onScanBuilt(self, scanInfoDict, signalDict, _devices):
        try:
            if self._simulation_mode:
                self.mockStartScan(scanInfoDict, signalDict)
            else:
                self.initiateScan(scanInfoDict, signalDict)
        except Exception as error:
            self.__logger.exception('APD scan preparation failed')
            try:
                self._teardownScan(raiseErrors=False)
            except Exception:
                self.__logger.exception(
                    'APD partial preparation cleanup failed'
                )
            self._reportScanBuildFailure('prepare', error)

    def _reportScanBuildFailure(self, stage, error):
        try:
            self._nidaqManager.reportScanBuildFailure(
                f'{self.name}.{stage}', error
            )
        except Exception:
            # A reporting failure must not let the original exception escape a
            # Qt slot, where several bindings treat it as process-fatal.
            self.__logger.exception(
                'Failed to report APD scan %s failure', stage
            )

    def mockStartScan(self, scanInfoDict, signalDict):
        self.initiateScan(scanInfoDict, signalDict)

    def mockStopScan(self):
        self._teardownScan(raiseErrors=False)

    def mockScanDone(self):
        return self._scanWorker is None or not getattr(
            self._scanWorker, "scanning", False
        )

    def startScan(self):
        try:
            thread = self._scanThread
            generation = self._preparedScanGeneration
            if (self._scanParticipating and generation is not None
                    and thread is not None and not thread.isRunning()):
                self._activeScanGeneration = generation
                thread.start()
        except Exception as error:
            self.__logger.exception('APD scan start failed')
            try:
                self._teardownScan(raiseErrors=False)
            except Exception:
                self.__logger.exception('APD partial start cleanup failed')
            self._reportScanBuildFailure('start', error)

    def startAcquisition(self):
        self.acquisition = True
        self.__newFrameReady = False

    def stopAcquisition(self):
        # Last-lease release is fail-closed even if teardown itself fails.
        # DetectorsManager will additionally quarantine a teardown failure.
        self.acquisition = False
        self._scanParticipating = False
        try:
            self._teardownScan(raiseErrors=True)
        except Exception as e:
            # Detector stop contract: teardown failure must reach the
            # DetectorsManager, which quarantines this detector as FAULTED.
            self.__logger.warning(f'Failed to stop acquisition cleanly: {e}')
            raise

    def stopAcquisitionLocal(self, generation=None):
        self._ensureScanLifecycleState()
        if generation is not None:
            with self._scanLifecycleLock:
                currentGeneration = getattr(
                    self._scanWorker, 'scanGeneration',
                    self._activeScanGeneration
                    or self._preparedScanGeneration,
                )
            if currentGeneration != generation:
                # A delayed queued completion from an old worker must never
                # detach a newer generation's worker/thread.
                self._completeScanGeneration(generation)
                return
        try:
            self._teardownScan(raiseErrors=False)
        except Exception as e:
            self.__logger.warning(f'Failed to stop acquisition locally: {e}')

    def _teardownScan(self, *, raiseErrors):
        """Detach first, then close; acknowledge only after all cleanup."""
        self._ensureScanLifecycleState()
        concurrentOperation = None
        with self._scanLifecycleLock:
            worker = self._scanWorker
            thread = self._scanThread
            generation = getattr(
                worker, 'scanGeneration',
                self._activeScanGeneration or self._preparedScanGeneration,
            )
            if worker is None and thread is None:
                operation = self._scanTeardownOperation
                if (operation is not None
                        and not operation['event'].is_set()):
                    concurrentOperation = operation
            else:
                operation = {
                    'event': threading.Event(),
                    'generation': generation,
                    'success': None,
                    'errors': (),
                    'errorsReported': False,
                }
                self._scanTeardownOperation = operation
                self._scanWorker = None
                self._scanThread = None
                if generation is not None:
                    self._tearingDownScanGenerations.add(generation)

        if worker is None and thread is None:
            if concurrentOperation is not None:
                return self._awaitTeardownOperation(
                    concurrentOperation, raiseErrors=raiseErrors
                )
            operation = self._scanTeardownOperation
            if operation is not None and operation['event'].is_set():
                return self._awaitTeardownOperation(
                    operation, raiseErrors=raiseErrors
                )
            if generation is not None:
                self._completeScanGeneration(generation)
            return True

        errors = []
        blockingCleanupFailed = False
        try:
            if worker is not None:
                try:
                    worker.scanning = False
                except Exception as error:
                    errors.append(error)
            if worker is not None:
                try:
                    # close() tears down the NI input task.  It must happen
                    # before joining the worker thread because a worker can be
                    # blocked inside the driver's read call; waiting first
                    # creates a teardown deadlock on abort and application
                    # shutdown.
                    worker.close()
                except Exception as error:
                    errors.append(error)
                    blockingCleanupFailed = True
            if thread is not None:
                try:
                    thread.quit()
                except Exception as error:
                    errors.append(error)
                    blockingCleanupFailed = True
                try:
                    _joinScanThreadBounded(
                        thread, _SCAN_THREAD_JOIN_TIMEOUT_MS, 'APD'
                    )
                except Exception as error:
                    errors.append(error)
                    blockingCleanupFailed = True

            if worker is not None:
                if self._ttlmultiplying:
                    try:
                        self._renewImage()
                    except Exception as error:
                        errors.append(error)
                try:
                    currSlice = self.__currSlice
                    if currSlice:
                        self.__currSlice = (
                            currSlice[:-1] + (currSlice[-1] + 1,)
                        )
                except (AttributeError, IndexError, TypeError):
                    # A build may fail before image bookkeeping exists.
                    pass
            # NOTE: do not set __newFrameReady here; d3Step already published
            # the final real frame and re-flagging creates a duplicate.
            if self._debug_mode:
                try:
                    plt.show()
                except Exception as error:
                    errors.append(error)
        finally:
            callbacks = ()
            with self._scanLifecycleLock:
                if blockingCleanupFailed:
                    # Keep strong references so Qt never destroys a
                    # still-running QThread and a later lease-release/finalize
                    # retry can attempt task closure again.
                    if self._scanWorker is None and self._scanThread is None:
                        self._scanWorker = worker
                        self._scanThread = thread
                elif generation is not None:
                    self._tearingDownScanGenerations.discard(generation)
                    self._completedScanGenerations.add(generation)
                    if self._activeScanGeneration == generation:
                        self._activeScanGeneration = None
                    if self._preparedScanGeneration == generation:
                        self._preparedScanGeneration = None
                    callbacks = self._finishAcks.pop(generation, ())
                operation['success'] = not blockingCleanupFailed
                operation['errors'] = tuple(errors)
                operation['event'].set()
            for acknowledge in callbacks:
                try:
                    acknowledge()
                except Exception:
                    self.__logger.exception(
                        'APD scan-finish acknowledgement failed'
                    )

        if errors:
            for error in errors:
                self.__logger.warning(
                    'APD scan teardown step failed: %s', error
                )
            if raiseErrors:
                operation['errorsReported'] = True
                raise errors[0]
        return not blockingCleanupFailed

    def _awaitTeardownOperation(self, operation, *, raiseErrors):
        if not operation['event'].wait(
                _SCAN_THREAD_JOIN_TIMEOUT_MS / 1000):
            error = RuntimeError(
                'APD scan teardown did not finish within '
                f'{_SCAN_THREAD_JOIN_TIMEOUT_MS / 1000:g} s'
            )
            if raiseErrors:
                raise error
            return False
        success = bool(operation['success'])
        errors = operation['errors']
        if errors and raiseErrors and not operation['errorsReported']:
            operation['errorsReported'] = True
            raise errors[0]
        if not success and raiseErrors:
            if errors:
                raise errors[0]
            raise RuntimeError('APD scan teardown failed')
        return success

    def _completeScanGeneration(self, generation):
        if generation is None:
            return
        self._ensureScanLifecycleState()
        with self._scanLifecycleLock:
            self._tearingDownScanGenerations.discard(generation)
            self._completedScanGenerations.add(generation)
            if self._activeScanGeneration == generation:
                self._activeScanGeneration = None
            if self._preparedScanGeneration == generation:
                self._preparedScanGeneration = None
            callbacks = self._finishAcks.pop(generation, ())
        for acknowledge in callbacks:
            try:
                acknowledge()
            except Exception:
                self.__logger.exception(
                    'APD scan-finish acknowledgement failed'
                )

    def finishScan(self, mode, acknowledge):
        self._ensureScanLifecycleState()
        if mode != 'graceful':
            # If a graceful barrier for this exact callback was installed
            # before the iteration escalated to abort, teardown must not invoke
            # it once and the abort path invoke it a second time.
            self.cancelFinishScan(acknowledge)
            with self._scanLifecycleLock:
                operation = self._scanTeardownOperation
                generation = (
                    self._activeScanGeneration
                    or self._preparedScanGeneration
                    or (
                        operation['generation']
                        if operation is not None else None
                    )
                )
                hasLocalWork = (
                    self._scanWorker is not None
                    or self._scanThread is not None
                    or (
                        operation is not None
                        and not operation['event'].is_set()
                    )
                )
                if generation is not None and hasLocalWork:
                    self._finishAcks.setdefault(
                        generation, []
                    ).append(acknowledge)
                    queued = True
                else:
                    queued = False
            succeeded = self._teardownScan(raiseErrors=False)
            if not queued:
                acknowledge()
            elif not succeeded and self.cancelFinishScan(acknowledge):
                # The bounded attempt failed. Let the coordinator release the
                # lease so strict stopAcquisition can quarantine the detector
                # instead of wedging forever when no timeout scheduler exists.
                acknowledge()
            return

        with self._scanLifecycleLock:
            operation = self._scanTeardownOperation
            generation = (
                self._activeScanGeneration
                or self._preparedScanGeneration
                or (
                    operation['generation']
                    if operation is not None else None
                )
            )
            hasLocalWork = (
                self._scanWorker is not None
                or self._scanThread is not None
                or generation in self._tearingDownScanGenerations
                or (
                    operation is not None
                    and not operation['event'].is_set()
                )
            )
            if (generation is None
                    or generation in self._completedScanGenerations
                    or not hasLocalWork):
                acknowledgeImmediately = True
            else:
                self._finishAcks.setdefault(generation, []).append(acknowledge)
                acknowledgeImmediately = False
        if acknowledgeImmediately:
            acknowledge()

    def cancelFinishScan(self, acknowledge):
        self._ensureScanLifecycleState()
        removed = False
        with self._scanLifecycleLock:
            for generation in tuple(self._finishAcks):
                callbacks = self._finishAcks[generation]
                remaining = []
                for callback in callbacks:
                    if callback is acknowledge:
                        removed = True
                    else:
                        remaining.append(callback)
                if remaining:
                    self._finishAcks[generation] = remaining
                else:
                    self._finishAcks.pop(generation, None)
        return removed

    def _ensureScanLifecycleState(self):
        """Initialize lifecycle fields for legacy/deserialized test objects."""
        state = object.__getattribute__(self, '__dict__')
        state.setdefault('_scanLifecycleLock', threading.Lock())
        state.setdefault('_scanGeneration', 0)
        state.setdefault('_preparedScanGeneration', None)
        state.setdefault('_activeScanGeneration', None)
        state.setdefault('_tearingDownScanGenerations', set())
        state.setdefault('_completedScanGenerations', set())
        state.setdefault('_finishAcks', {})
        state.setdefault('_scanTeardownOperation', None)

    def getLatestFrame(self, is_save=True):
        S = int(getattr(self, "_linestep", 1))
        if is_save or S > 1:
            return self._image  # raw stack (1,S,Ny,Nx)
        return self._image_display  # always (1,Ny,Nx)

    def _renewImage(self):
        im_squeezed, ax_rem = self.remove_nans(self._image)
        self.setShape(np.shape(im_squeezed))
        self._image = im_squeezed
        px_sizes = self.__pixel_sizes.copy()[::-1]
        for axis in ax_rem:
            px_sizes.pop(axis)
        self.setPixelSize(px_sizes[::-1])

    def _convertPixelsForImage(self, pixels):
        pixels = np.asarray(pixels)
        if np.issubdtype(self._image.dtype, np.integer):
            info = np.iinfo(self._image.dtype)
            rounded = np.rint(
                np.nan_to_num(
                    pixels,
                    nan=0.0,
                    posinf=float(info.max),
                    neginf=float(info.min),
                )
            )
            clipped = np.clip(rounded, info.min, info.max)
            if (not self._warned_mock_count_clip
                    and np.any(clipped != rounded)):
                self.__logger.warning(
                    f'Clipped APD pixels to {info.min}..{info.max} '
                    f'before writing {self._image.dtype} image buffer.'
                )
                self._warned_mock_count_clip = True
            return clipped.astype(self._image.dtype, copy=False)
        return pixels.astype(self._image.dtype, copy=False)

    def updateImage(self, pixels, pos: tuple):
        """
        pos is emitted as tuple(np.flip(self._pos[1:])) from ScanWorker.
        For XY:           pos == (y,)
        For Z stacks:     pos == (z, y)
        For higher dims:  pos == (..., z, y)  (last element is always y_expanded)
        """
        if not hasattr(self, "_linestep"):
            self._linestep = 1
        S = int(self._linestep)

        # last entry is the (expanded) line index along Y
        y_expanded = int(pos[-1])

        # clip pixel write length to Nx
        Nx = self._image.shape[-1] if self._image.size else len(pixels)
        n = min(len(pixels), Nx)

        if S > 1:
            # raw buffer for 2D+linestep is typically: (1, S, Ny, Nx), but can be (1, S, Nz, Ny, Nx)
            y = y_expanded // S
            s = y_expanded % S
        else:
            y = y_expanded
            s = None

        # ---- S == 1: could be 2D (1, Ny, Nx) OR 3D (1, Nz, Ny, Nx) (or higher) ----
        if np.squeeze(self._image).ndim == 2:
            # (1, Ny, Nx)
            Ny = self._image.shape[-2]
            if y >= Ny:
                return
            converted = self._convertPixelsForImage(pixels[:n])
            # Index the last two axes (..., y, x): the buffer is (Ny, Nx) for a
            # true 2D scan but (1, Ny, Nx) for a single-plane 3D scan (e.g. Nz=1),
            # which squeezes to ndim 2 above. Plain [y, :n] would index the
            # leading singleton axis and raise IndexError for y >= 1.
            self._image[..., y, :n] = converted
            self.__currSlice = (y_expanded,)
            # Time-throttled live preview: bound the redraw rate deterministically
            # instead of gating on image size with a random draw per line.
            if self._liveThrottle.due():
                self.sigImageUpdated.emit(self._image, True, self.scale)
            return

        if np.squeeze(self._image).ndim >= 3:
            # (Nz, Ny, Nx) for Z stacks (and potentially more dims in front of Ny,Nx)
            # pos[:-1] contains all outer indices (e.g. z), last is y
            outer = tuple(int(v) for v in pos[:-1])  # e.g. (z,) or (t,z,...) depending on scan
            # Build index into raw buffer:
            # raw layout is (1, ...outer..., y, x)
            # where y is always the second-to-last axis
            if s is not None:
                idx = (s,) + outer + (y, slice(0, n))
            else:
                idx = outer + (y, slice(0, n))
            converted = self._convertPixelsForImage(pixels[:n])
            self._image[idx] = converted
            self.__currSlice = outer + (y,)
            return

    def initiateImage(self, img_dims):
        """
        img_dims comes from ScanWorker._output_image_dims:
          - 2D, S==1: (Nx, Ny)
          - 2D, S>1 : (Nx, Ny, S)
          - 3D Z    : (Nx, Ny, Nz)   (S==1)
          - 3D Z + S: (Nx, Ny, Nz, S)
        Raw buffer is allocated as reversed + leading 1.
        """
        img_dims = tuple(int(x) for x in img_dims)

        img_dims_extra = tuple(reversed(img_dims))

        # Use uint16 for the common photon-counting case; the TTL-multiplying
        # path inserts NaN as a "no-data" marker, so fall back to float32 there
        # (still ImageJ-compatible, half the memory of float64).
        image_dtype = np.float32 if self._ttlmultiplying else np.uint16

        if (np.shape(self._image) != img_dims_extra
                or self._image.dtype != image_dtype):
            self._image = np.zeros(img_dims_extra, dtype=image_dtype)
            self.setShape(img_dims_extra)

        self._image_display = np.zeros(
            tuple([int(img_dims[i]) for i in range(max(len(img_dims), 2))]),
            dtype=image_dtype,
        )

    def setParameter(self, name, value):
        pass

    def getParameter(self, name):
        pass

    def setBinning(self, binning):
        super().setBinning(binning)

    def getChunk(self):
        if not self.__newFrameReady:
            return np.empty((0, 0, 0), dtype=self.dtype)
        self.__newFrameReady = False
        return np.expand_dims(self._image_display, axis=0).copy()

    def flushBuffers(self):
        self.__newFrameReady = False

    def _acceptScanWorkerGeneration(self, generation):
        """Whether a queued worker callback still belongs to local state."""
        if generation is None:
            return True
        with self._scanLifecycleLock:
            operation = self._scanTeardownOperation
            operationActive = (
                operation is not None
                and operation.get('generation') == generation
                and not operation['event'].is_set()
            )
            return (
                self._activeScanGeneration == generation
                or self._preparedScanGeneration == generation
                or operationActive
            ) and generation not in self._completedScanGenerations

    def _onScanPixels(self, pixels, pos, generation):
        if self._acceptScanWorkerGeneration(generation):
            self.updateImage(pixels, pos)

    def _onFrameBoundary(self, generation=None):
        """
        Called by ScanWorker.d3Step at the end of a full frame.
        Updates display buffer and triggers GUI redraw once per frame.
        """
        if not self._acceptScanWorkerGeneration(generation):
            if int(getattr(self, "_linestep", 1)) > 1:
                self.__logger.warning(
                    "[LineStepDiag][APD:%s] frame_boundary_rejected "
                    "generation=%s active=%s prepared=%s completed=%s",
                    self._name,
                    generation,
                    self._activeScanGeneration,
                    self._preparedScanGeneration,
                    sorted(self._completedScanGenerations),
                )
            return
        if self._image_display.size == 0:
            return

        S = int(getattr(self, "_linestep", 1))
        if S > 1:
            self.__logger.info(
                "[LineStepDiag][APD:%s] frame_boundary_received "
                "generation=%s raw_shape=%s",
                self._name,
                generation,
                np.shape(self._image),
            )
        if S > 1:
            raw = np.squeeze(self._image)  # (S,..,Ny,Nx)
            mode = getattr(self, "_linestep_view_mode", None)
            if mode == "max":
                im = np.nanmax(raw, axis=0)
            elif mode == "slice":
                idx = int(getattr(self, "_linestep_view_index", 0)) % raw.shape[0]
                im = raw[idx]
            elif mode == "sum":
                im = np.nansum(raw, axis=0)
            else:
                im = raw
        else:
            im = np.squeeze(self._image)

        while im.ndim > self._image_display.ndim:
            im = np.squeeze(im[0])

        self._image_display = im
        # Publish the boundary before asking the shared latest-frame broker to
        # drain it.  If a previous frame was still flagged, doing this in the
        # opposite order drains the new pixels under the old flag and then
        # re-flags those same pixels, duplicating one frame for every consumer.
        self.__newFrameReady = True
        self.updateLatestFrame(True)
        self.sigNewFrame.emit()
        if S > 1:
            self.__logger.info(
                "[LineStepDiag][APD:%s] frame_published generation=%s "
                "published_shape=%s",
                self._name,
                generation,
                np.shape(self._image_display),
            )

    @property
    def shape(self):
        return self.__shape

    def setShape(self, img_dims):
        self.__shape = tuple(img_dims)

    @property
    def isScanDriven(self):
        return True

    @property
    def scale(self):
        return self.__pixel_sizes[::-1]

    @property
    def pixelSizeUm(self):
        return [1, *self.__pixel_sizes]

    def setPixelSize(self, pixel_sizes: list):
        # pixel_sizes: list of low dim to high dim
        self.__pixel_sizes = pixel_sizes

    @property
    def dtype(self):
        """ Override: APD's recorded dtype mirrors initiateImage buffer choice. """
        return np.dtype(np.float32) if self._ttlmultiplying else np.dtype(np.uint16)

    def crop(self, hpos, vpos, hsize, vsize):
        pass

    def remove_nans(self, im):
        """ Remove slices which only contain np.nan values, called at end of acquisition.
        Source: https://stackoverflow.com/a/43724800 """
        acc = np.maximum.accumulate
        m = ~np.isnan(im)
        dims = im.ndim

        if dims == 1:
            return im[acc(m) & acc(m[::-1])[::-1]]
        else:
            r = np.tile(np.arange(dims), dims)
            per_axis_combs = np.delete(r, range(0, len(r), dims + 1)).reshape(-1, dims - 1)
            per_axis_combs_tuple = map(tuple, per_axis_combs)

            mask = []
            for i in per_axis_combs_tuple:
                m0 = m.any(i)
                mask.append(acc(m0) & acc(m0[::-1])[::-1])
            im_ret = im[np.ix_(*mask)]
            ax_rem = [i for i, val in enumerate(np.shape(im_ret)[1:]) if val == 1]
            return np.expand_dims(np.squeeze(im_ret), axis=0).astype(int), ax_rem


class ScanWorker(Worker):
    d2Step = Signal(np.ndarray, tuple, int)
    d3Step = Signal(int)
    acqDoneSignal = Signal(int)

    def __init__(self, manager, scanInfoDict, signalDict):
        super().__init__()
        self.__logger = initLogger(self, tryInheritParent=True)

        self._samples_read = 0
        self._last_value = 0
        self._manager = manager
        self._name = self._manager._name
        self._channel = self._manager._channel
        self.scanGeneration = int(
            getattr(manager, '_preparedScanGeneration', 0) or 0
        )
        self._inputTaskGeneration = None
        self._inputTaskClosed = False

        # time step of scanning, in s
        self._scan_dwell_time = scanInfoDict['dwell_time']

        # ratio between detection sampling time and pixel dwell time (has nothing to do with
        # sampling of scanning line)
        self._frac_det_dwell = max(
            1,
            int(round(self._scan_dwell_time * self._manager._detection_samplerate)),
        )

        # ratio between detection sample rate and scanning sample rate
        self._frac_scan_det_rate = max(
            1,
            int(round(self._manager._detection_samplerate * scanInfoDict['scan_time_step'])),
        )
        self._rng = np.random.default_rng(self._manager._mock_random_seed)

        # extract APD signals from signalDict
        self._seq_signal = None  # set only if ttlmultiplying AND this device found in signalDict
        if self._manager._ttlmultiplying:
            for target in signalDict['TTLCycleSignalsDict'].keys():
                if self._name == target:
                    self._seq_signal = np.repeat(signalDict['TTLCycleSignalsDict'][target].copy(),
                                                 self._frac_scan_det_rate)
                    self._seq_signal = self._seq_signal.astype('float')
                    self._seq_signal[self._seq_signal == 0] = np.nan
                    break

        # img_dims contains physical scan axes only (no linestep); n_linesteps is separate.
        scan_dims = list(scanInfoDict["img_dims"])
        scan_axes = list(scanInfoDict.get("img_axes_phys", ["x", "y", "z"][:len(scan_dims)]))
        self._linestep = max(1, int(scanInfoDict.get("n_linesteps", 1)))

        # Y is the slow scan axis
        y_idx = scan_axes.index("y") if "y" in scan_axes else 1
        self._y_idx = y_idx

        # Loop dims: expand Y by linestep (Ny -> Ny*S), keep other dims unchanged
        self._img_dims = scan_dims  # recursion uses physical scan axes only (x,y,z,...)
        self._loop_dims = scan_dims.copy()
        self._loop_dims[y_idx] = int(self._loop_dims[y_idx] * self._linestep)

        # Output dims for manager allocation: keep linestep as its own axis (for later stack/sum/max)
        # We append linestep as the LAST axis in output_image_dims (manager can reorder if desired)
        self._output_image_dims = tuple(scan_dims + ([self._linestep] if self._linestep > 1 else []))

        # det samples per scan steps in different dims
        self._samples_d_scanstep = [round(samples) * self._frac_scan_det_rate for samples in
                                    scanInfoDict['scan_samples']]
        # det samples per fast axis period
        self._samples_d2_period = round(scanInfoDict['scan_samples_d2_period'] * self._frac_scan_det_rate)
        # det samples in total signal
        self._samples_total = round(scanInfoDict['scan_samples_total'] * self._frac_scan_det_rate)
        # samples to throw due to:
        self._throw_startzero = round(
            scanInfoDict.get('scan_throw_startzero', 0) * self._frac_scan_det_rate)  # starting zero-padding
        self._scan_pads_initpos = [round(initpos) * self._frac_scan_det_rate for initpos in
                                   scanInfoDict.get('scan_pads_initpos', [])]  # smooth inital positioning times
        self._throw_settling = round(scanInfoDict.get('scan_throw_settling', 0) * self._frac_scan_det_rate)  # settling time
        self._throw_startacc = round(
            scanInfoDict.get('scan_throw_startacc', 0) * self._frac_scan_det_rate)  # starting acceleration

        self._phase_delay = int(scanInfoDict.get('phase_delay', 0))  # phase delay samples - galvo response time
        self._smooth_axes = scanInfoDict.get('smooth_axes', [False, False, False])

        # samples to throw due to smooth between d>2 step transitioning
        pad_initpos = self._scan_pads_initpos[0] if len(self._scan_pads_initpos) > 0 else 0
        self._throw_init_smooth = (pad_initpos + self._throw_settling + self._throw_startacc)
        # initiate parameter for thrown samples for smooth higher dimensions step init
        self._throw_init_higher_d = False
        self._pending_read = None

        self._logLinestepReadPlan(scanInfoDict)

        if not self._manager._simulation_mode:
            self._inputTaskGeneration = (
                self._manager._nidaqManager.startInputTask(
                    self._name, 'ci', self._channel, 'finite',
                    self._manager._nidaq_clock_source,
                    self._manager._detection_samplerate,
                    self._samples_total, True, 'ao/StartTrigger',
                    self._manager._terminal,
                )
            )
        self._manager.initiateImage(self._output_image_dims)
        self._manager.setPixelSize(scanInfoDict['pixel_sizes'])  # 'pixel_sizes' order: low dim to high dim

    def _logLinestepReadPlan(self, scanInfoDict):
        if self._linestep <= 1:
            return

        expandedLines = int(self._loop_dims[self._y_idx])
        exact2dPlan = None
        exact2dMargin = None
        if len(self._loop_dims) == 2:
            higherInit = 0
            if len(self._scan_pads_initpos) > 1 and any(
                np.greater(
                    self._scan_pads_initpos[1:],
                    self._scan_pads_initpos[0],
                )
            ):
                higherInit = int(
                    np.max(self._scan_pads_initpos[1:])
                    - self._scan_pads_initpos[0]
                )
            exact2dPlan = int(
                self._phase_delay
                + self._throw_startzero
                + higherInit
                + self._throw_init_smooth
                + max(0, expandedLines - 1) * self._samples_d2_period
                + self._samples_d_scanstep[1]
            )
            exact2dMargin = int(self._samples_total - exact2dPlan)

        mismatch = (
            exact2dMargin is not None and exact2dMargin < 0
        )
        self.__logger.info(
            "[LineStepDiag][APD:%s] scan_S=%s img_dims=%s "
            "loop_dims=%s output_dims=%s expanded_Y_lines=%s "
            "scan_samples_total=%s CI_finite_samples=%s "
            "line_samples=%s line_period_samples=%s "
            "initial_throw_samples=%s exact_2D_read_plan=%s "
            "remaining_samples=%s status=%s",
            self._name,
            self._linestep,
            scanInfoDict.get("img_dims"),
            self._loop_dims,
            self._output_image_dims,
            expandedLines,
            scanInfoDict.get("scan_samples_total"),
            self._samples_total,
            (
                self._samples_d_scanstep[1]
                if len(self._samples_d_scanstep) > 1 else None
            ),
            self._samples_d2_period,
            int(
                self._phase_delay
                + self._throw_startzero
                + self._throw_init_smooth
            ),
            exact2dPlan,
            exact2dMargin,
            (
                "MISMATCH: APD read plan exceeds finite task"
                if mismatch else "OK"
            ),
        )

    def throwdata(self, datalen):
        """ Throw away data with length datalen, save the last value,
        and add length of data to total samples_read length.
        """
        if datalen > 0:
            self._pending_read = {
                "kind": "throw",
                "start": int(self._samples_read),
                "count": int(datalen),
            }
            if self._manager._simulation_mode:
                throwdata = self.randomInput(datalen)
            else:
                throwdata = self._manager._nidaqManager.readInputTask(
                    self._name, datalen,
                    generation=self._inputTaskGeneration,
                )
            if self._manager._debug_mode:
                self.__plot_curves(plot=True, xvals=range(int((self._samples_read) / 10),
                                                          int((self._samples_read + datalen) / 10)),
                                   signal=self._ploty * np.ones(int((datalen) / 10)),
                                   style='r-')
            self._last_value = throwdata[-1]
            self._samples_read += datalen
            self._pending_read = None

    def readdata(self, datalen):
        """ Read data with length datalen and add length of data to total samples_read length.
        """
        self._pending_read = {
            "kind": "line",
            "start": int(self._samples_read),
            "count": int(datalen),
        }
        if self._manager._simulation_mode:
            data = self.randomInput(datalen)
        else:
            data = self._manager._nidaqManager.readInputTask(
                self._name, datalen,
                generation=self._inputTaskGeneration,
            )
        if self._manager._debug_mode:
            self.__plot_curves(plot=True, xvals=range(int((self._samples_read) / 10),
                                                      int((self._samples_read + datalen) / 10)),
                               signal=self._ploty * np.ones(int((datalen) / 10)),
                               style='k-')
        self._samples_read += datalen
        self._pending_read = None
        return data

    def samples_to_pixels(self, line_samples):
        """ Reshape read datastream over the line to a line with pixel counts.
        Do this by summing elements, with the rate ratio calculated previously.
        """
        frac = int(self._frac_det_dwell)
        n = (len(line_samples) // frac) * frac
        if n == 0:
            return np.zeros((0,), dtype=self._manager.dtype)
        if n != len(line_samples):
            # optional: logger warning
            line_samples = line_samples[:n]
        pixels = np.asarray(line_samples).reshape(-1, frac).sum(axis=1)
        if not self._manager._ttlmultiplying:
            pixels = np.clip(pixels, 0, self._manager._mock_photon_count_max)
        return pixels

    def __plot_curves(self, plot, xvals, signal, style='k-'):
        """ Plot read and thrown samples, for debugging. """
        if plot:
            plt.figure(1)
            plt.plot(xvals, signal, style)
            self._ploty += 0.01
            if self._ploty > 1.1:
                self._ploty = 1

    def run(self):
        try:
            if self._linestep > 1:
                self.__logger.info(
                    "[LineStepDiag][APD:%s] worker_start generation=%s "
                    "samples_read=%s/%s loop_dims=%s",
                    self._name,
                    self.scanGeneration,
                    self._samples_read,
                    self._samples_total,
                    self._loop_dims,
                )
            self._runAcquisition()
            if self._linestep > 1:
                self.__logger.info(
                    "[LineStepDiag][APD:%s] worker_complete generation=%s "
                    "samples_read=%s/%s final_pos=%s",
                    self._name,
                    self.scanGeneration,
                    self._samples_read,
                    self._samples_total,
                    getattr(self, "_pos", None),
                )
        except Exception:
            # An exception escaping a Qt worker slot can terminate the process.
            # Convert it into the same generation-tagged local completion path
            # used by a normal scan instead.
            self.__logger.exception(
                "[LineStepDiag][APD:%s] worker_failed generation=%s "
                "samples_read=%s/%s pos=%s pending_read=%s",
                self._name,
                self.scanGeneration,
                self._samples_read,
                self._samples_total,
                getattr(self, "_pos", None),
                self._pending_read,
            )
        finally:
            self.scanning = False
            self.acqDoneSignal.emit(int(self.scanGeneration))

    def _runAcquisition(self):
        """ Main run for acquisition.
        """
        if self._manager._debug_mode:
            self._ploty = 1
        # create empty current position counter
        self._pos = np.zeros(len(self._loop_dims), dtype='uint16')
        # throw away phase delay samples and start zero samples
        self.throwdata(self._phase_delay)
        self.throwdata(self._throw_startzero)
        # throw away higher dim initpos, if any
        if len(self._scan_pads_initpos) > 1:
            if any(np.greater(self._scan_pads_initpos[1:], self._scan_pads_initpos[0])):
                self._throw_init_higher_d = np.max(self._scan_pads_initpos[1:]) - self._scan_pads_initpos[0]
                self.throwdata(self._throw_init_higher_d)
        if len(self._loop_dims) == 2:
            # begin d3 step: throw data from initial d3 step positioning
            self.throwdata(self._throw_init_smooth)
        # loop through all dimensions to record data, starting with the outermost dimension
        self.run_loop_dx(dim=len(self._loop_dims))

    def run_loop_dx(self, dim):
        """ Recursive looping through all scanning dimensions, actually read samples at dim = 2,
        and step through all steps in each dimension.
        Works for arbitrary amount of dimensions, tested for <=5.
        """
        while self._pos[dim - 1] < self._loop_dims[dim - 1]:
            if dim > 2:
                if dim == 3:
                    if any(self._smooth_axes[:dim - 1]) or self._pos[dim - 1] == 0:
                        # begin d step: throw data from initial smooth step positioning,
                        # if this is the first smooth axis, or if it is the first step on the axis
                        self.throwdata(self._throw_init_smooth)
                self.run_loop_dx(dim - 1)
                if dim >= 3:
                    # end higher dim step: realign actual N read samples with supposed N read samples,
                    # compensating for all axis initpos and finalpos
                    pos = np.copy(self._pos)
                    pos[dim - 1] += 1
                    # supposed samples = start_zero_samples + d_steps * d samples_per_step
                    supposed_samples_read = self._throw_startzero + np.sum(
                        np.multiply(pos, self._samples_d_scanstep[:-1]))
                    if self._throw_init_higher_d:
                        # if some smooth higher dim init, add those samples to the supposedly read samples
                        supposed_samples_read += self._throw_init_higher_d
                    throwdatalen = supposed_samples_read - (self._samples_read - self._phase_delay)
                    if throwdatalen > 0:
                        self.throwdata(throwdatalen)
                    if dim == 3:
                        self.d3Step.emit(self.scanGeneration)
            else:
                self.run_loop_d2()
            self._pos[dim - 1] += 1
        self._pos[dim - 1] = 0

    def run_loop_d2(self):
        """ Reading data on dim = 2, changing data to pixels, and emitting the d2 step of pixels.
        """
        if self.scanning:
            if self._pos[1] == self._loop_dims[1] - 1:
                # read a line
                if self._manager._ttlmultiplying:
                    seq_signal_xstart = self._samples_read - self._phase_delay
                data = self.readdata(self._samples_d_scanstep[1])
                if self._manager._ttlmultiplying:
                    seq_signal_xend = self._samples_read - self._phase_delay
                    ttl_seq = self._seq_signal[seq_signal_xstart:seq_signal_xend]
            else:
                # read a whole period, starting with the line and then the data during the flyback
                if self._manager._ttlmultiplying:
                    seq_signal_xstart = self._samples_read - self._phase_delay
                data = self.readdata(self._samples_d2_period)
                if self._manager._ttlmultiplying:
                    seq_signal_xend = self._samples_read - self._phase_delay
                    ttl_seq = self._seq_signal[seq_signal_xstart:seq_signal_xend]
            # get photon counts from data array (which is cumsummed)
            data_cnts = np.concatenate(([data[0] - self._last_value], np.diff(data)))
            self._last_value = data[-1]
            # only take the first samples that corresponds to the samples during the line
            line_samples = data_cnts[:self._samples_d_scanstep[1]]
            if self._manager._ttlmultiplying and self._seq_signal is not None:
                ttl_seq = ttl_seq[:self._samples_d_scanstep[1]]
                # mask with TTL sequence from ScanWidget, to say if detector should be on or not
                line_samples = np.multiply(line_samples, 1 * ttl_seq)
            # resample sample array to pixel counts array
            pixels = self.samples_to_pixels(line_samples)
            # signal new line of pixels, and the insertion position in all dimensions
            self.d2Step.emit(
                pixels,
                tuple(np.flip(self._pos[1:])),
                self.scanGeneration,
            )
            if len(self._loop_dims) == 2 and self._pos[1] == self._loop_dims[1] - 1:
                self.d3Step.emit(self.scanGeneration)
        else:
            self.__logger.debug('Close data reading: not scanning any longer')
            self.close()

    def close(self):
        if not self._manager._simulation_mode and not self._inputTaskClosed:
            self._manager._nidaqManager.inputTaskDone(
                self._name, self._inputTaskGeneration
            )
            self._inputTaskClosed = True

    def randomInput(self, datalen):
        datalen = int(datalen)
        if datalen <= 0:
            return np.empty((0,), dtype=np.int64)

        frac = max(1, int(self._frac_det_dwell))
        mean_per_sample = max(0.0, self._manager._mock_photon_count_mean / frac)
        max_per_sample = max(1, int(np.ceil(self._manager._mock_photon_count_max / frac)))
        increments = self._rng.poisson(mean_per_sample, size=datalen).astype(np.int64)
        increments = np.clip(increments, 0, max_per_sample)
        return np.cumsum(increments, dtype=np.int64) + int(self._last_value)

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
