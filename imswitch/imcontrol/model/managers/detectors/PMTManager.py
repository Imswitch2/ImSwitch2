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


class PMTManager(DetectorManager):
    """PMT analog-input manager with linestep-aware buffering + frame-boundary UI updates."""

    def __init__(self, detectorInfo, name, nidaqManager, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)

        model = name
        self._name = name

        self._image = np.array([])          # raw buffer (stack if linesteps > 1)
        self._image_display = np.array([])  # always (1, Ny, Nx)
        self.__newFrameReady = False

        # Pixel sizes stored low-dim to high-dim (ImSwitch convention used elsewhere)
        self.__pixel_sizes = [1, 1]

        self._detection_samplerate = float(1e6)
        self._nidaq_clock_source = r"ctr2InternalOutput"

        manager_props = detectorInfo.managerProperties
        self._channel = manager_props.get("analogInputLine", None)
        device_name = manager_props.get("deviceName", "Dev1")
        if isinstance(self._channel, int):
            self._channel = f"{device_name}/ai{self._channel}"

        self._offset_v = float(manager_props.get("offset_v", 0.0))
        self._mock_voltage_min = float(
            manager_props.get("mockVoltageMin", manager_props.get("mock_voltage_min", -5.0))
        )
        self._mock_voltage_max = float(
            manager_props.get("mockVoltageMax", manager_props.get("mock_voltage_max", 5.0))
        )
        if self._mock_voltage_min > self._mock_voltage_max:
            self._mock_voltage_min, self._mock_voltage_max = (
                self._mock_voltage_max,
                self._mock_voltage_min,
            )
        self._mock_voltage_mean = float(
            manager_props.get("mockVoltageMean", manager_props.get("mock_voltage_mean", 0.0))
        )
        self._mock_voltage_noise_std = max(
            0.0,
            float(
                manager_props.get(
                    "mockVoltageNoiseStd",
                    manager_props.get("mock_voltage_noise_std", 0.5),
                )
            ),
        )
        self._mock_random_seed = manager_props.get(
            "mockRandomSeed",
            manager_props.get("mock_random_seed", None),
        )

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

        # Deterministic rate limiter for the in-progress live preview (see
        # APDManager): caps full-image napari redraws at a fixed rate instead
        # of the old shape-dependent random gate. Tunable per rig via the
        # ``liveUpdateIntervalMs`` config property (default 50 ms = 20 Hz).
        self._liveThrottle = LiveDisplayThrottle(
            min_interval_s=max(
                0.0,
                float(manager_props.get("liveUpdateIntervalMs", 50.0)) / 1000.0,
            )
        )

        self._ttlmultiplying = False
        # Generate detected samples instead of reading the NI-DAQ analog input.
        # Forced on whenever the NI-DAQ itself is simulating: with no hardware
        # the input task is None, so reading it would crash
        # (startInputTask -> None.start()). An explicit config flag can also turn
        # it on against a real NI-DAQ for bench testing.
        self._simulation_mode = bool(
            manager_props.get("simulation_mode", False)
            or getattr(nidaqManager, 'isSimulated', False)
        )
        self._debug_mode = False

        # linestep settings (kept for manager-side display logic)
        self._linestep = 1
        self._linestep_view_mode = "sum"   # "sum" | "max" | "slice"
        self._linestep_view_index = 0      # used if mode == "slice"

        self.acquisition = True

        parameters = {}
        self._nidaqManager = nidaqManager
        self._nidaqManager.sigScanBuilt.connect(
            self._onScanBuilt
        )
        self._nidaqManager.sigScanStarted.connect(self.startScan)

        self.__shape = (100, 100)  # or self.fullShape if you prefer

        super().__init__(
            detectorInfo, name,
            fullShape=(100, 100),
            supportedBinnings=[1],
            model=model,
            parameters=parameters,
            croppable=False
        )

    def crop(self, hpos, vpos, hsize, vsize):
        pass

    def __del__(self):
        try:
            self._teardownScan(raiseErrors=False)
        except Exception as e:
            try:
                state = object.__getattribute__(self, '__dict__')
            except Exception:
                state = {}
            logger = state.get('_PMTManager__logger')
            if logger is not None:
                logger.warning(f'Failed to clean up scan thread: {e}')
        if hasattr(super(), "__del__"):
            super().__del__()

    @property
    def isScanDriven(self):
        return True

    @property
    def pixelSizeUm(self):
        # return [t, y, x] scale style
        return [1, self.__pixel_sizes[1], self.__pixel_sizes[0]]

    @property
    def scale(self):
        return self.__pixel_sizes[::-1]

    def setPixelSize(self, pixel_sizes: list):
        self.__pixel_sizes = list(pixel_sizes)

    @property
    def dtype(self):
        """ Override: PMT analog voltage buffer is always float32. """
        return np.dtype(np.float32)

    def initiateScan(self, scanInfoDict, signalDict):
        participants = scanInfoDict.get(PARTICIPANTS_KEY)
        self._scanParticipating = (
            participants is None or self.name in participants
        )
        self._preparedScanGeneration = None
        if not self._scanParticipating:
            return

        # The scan-scoped participant snapshot is authoritative.  The
        # acquisition flag describes coarse hardware state and historically
        # remained true after last-lease teardown.
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
        self._scanWorker.d2Step.connect(self._onScanPixels)
        self._scanWorker.d3Step.connect(self._onFrameBoundary)
        self._scanWorker.acqDoneSignal.connect(self.stopAcquisitionLocal)

        # store linestep on manager for display logic
        self._linestep = int(getattr(self._scanWorker, "_linestep", 1))

        if self._debug_mode:
            plt.figure(1)
        self._preparedScanGeneration = generation

    def _onScanBuilt(self, scanInfoDict, signalDict, _devices):
        try:
            if self._simulation_mode:
                self.mockStartScan(scanInfoDict, signalDict)
            else:
                self.initiateScan(scanInfoDict, signalDict)
        except Exception as error:
            self.__logger.exception('PMT scan preparation failed')
            try:
                self._teardownScan(raiseErrors=False)
            except Exception:
                self.__logger.exception(
                    'PMT partial preparation cleanup failed'
                )
            self._reportScanBuildFailure('prepare', error)

    def _reportScanBuildFailure(self, stage, error):
        try:
            self._nidaqManager.reportScanBuildFailure(
                f'{self.name}.{stage}', error
            )
        except Exception:
            self.__logger.exception(
                'Failed to report PMT scan %s failure', stage
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
            self.__logger.exception('PMT scan start failed')
            try:
                self._teardownScan(raiseErrors=False)
            except Exception:
                self.__logger.exception('PMT partial start cleanup failed')
            self._reportScanBuildFailure('start', error)

    def startAcquisition(self):
        self.acquisition = True
        self.__newFrameReady = False

    def stopAcquisition(self):
        # Detector stop contract: teardown failure must reach the
        # DetectorsManager, which quarantines this detector as FAULTED.
        # Mark the manager fail-closed even if teardown raises.
        self.acquisition = False
        self._scanParticipating = False
        self._teardownScan(raiseErrors=True)

    def stopAcquisitionLocal(self, generation=None):
        # Internal worker-completion path (ScanWorker.acqDoneSignal): no lease
        # operation is in flight to report a fault to, so this one still
        # swallows and logs.
        self._ensureScanLifecycleState()
        if generation is not None:
            with self._scanLifecycleLock:
                currentGeneration = getattr(
                    self._scanWorker, 'scanGeneration',
                    self._activeScanGeneration
                    or self._preparedScanGeneration,
                )
            if currentGeneration != generation:
                self._completeScanGeneration(generation)
                return
        try:
            self._teardownScan(raiseErrors=False)
        except Exception:
            self.__logger.exception("Error stopping PMT acquisition")

    def _teardownScan(self, *, raiseErrors=True):
        """ Tear down the scan worker/thread. Raises on failure — see the
        detector stop contract in DetectorManager.stopAcquisition. """
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
                    # Closing the NI input task is what unblocks a worker
                    # waiting in the driver's read call.  Joining before this
                    # close can deadlock abort and application shutdown.
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
                        thread, _SCAN_THREAD_JOIN_TIMEOUT_MS, 'PMT'
                    )
                except Exception as error:
                    errors.append(error)
                    blockingCleanupFailed = True

            if self._ttlmultiplying:
                try:
                    self._renewImage()
                except Exception as error:
                    errors.append(error)

            # NOTE: do NOT call _onFrameBoundary() here. d3Step at scan end has
            # already produced the final _image_display and flagged
            # __newFrameReady; re-running it re-flags the same data and causes
            # getChunk() to deliver the last frame a second time (phantom
            # duplicate in the recorded file).

        finally:
            if self._debug_mode:
                try:
                    plt.show()
                except Exception as error:
                    errors.append(error)
            callbacks = ()
            with self._scanLifecycleLock:
                if blockingCleanupFailed:
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
                        'PMT scan-finish acknowledgement failed'
                    )

        if errors:
            for error in errors:
                self.__logger.warning(
                    'PMT scan teardown step failed: %s', error
                )
            if raiseErrors:
                operation['errorsReported'] = True
                raise errors[0]
        return not blockingCleanupFailed

    def _awaitTeardownOperation(self, operation, *, raiseErrors):
        if not operation['event'].wait(
                _SCAN_THREAD_JOIN_TIMEOUT_MS / 1000):
            error = RuntimeError(
                'PMT scan teardown did not finish within '
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
            raise RuntimeError('PMT scan teardown failed')
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
                    'PMT scan-finish acknowledgement failed'
                )

    def finishScan(self, mode, acknowledge):
        self._ensureScanLifecycleState()
        if mode != 'graceful':
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

        # PMT data is analog voltage — float32 has ~7 decimal digits, plenty
        # for noise-limited PMT signals, and halves memory vs float64. NaN is
        # preserved for the TTL-multiplying "no-data" marker.
        image_dtype = np.float32

        if (np.shape(self._image) != img_dims_extra
                or self._image.dtype != image_dtype):
            self._image = np.zeros(img_dims_extra, dtype=image_dtype)
            self.setShape(img_dims_extra)

        self._image_display = np.zeros(
            tuple([int(img_dims[i]) for i in range(max(len(img_dims), 2))]),
            dtype=image_dtype,
        )


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
            # Index the last two axes (..., y, x): the buffer is (Ny, Nx) for a
            # true 2D scan but (1, Ny, Nx) for a single-plane 3D scan (Nz=1),
            # which squeezes to ndim 2 above. Plain [y, :n] would index the
            # leading singleton axis and raise IndexError for y >= 1.
            self._image[..., y, :n] = pixels[:n]
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
            self._image[idx] = pixels[:n]
            self.__currSlice = outer + (y,)
            return

    def _compute_display_frame(self):
        """
        Build a 2D plane (Ny, Nx) from the raw buffer.
        Keeps future flexibility: sum/max/slice over linestep.
        """
        S = int(getattr(self, "_linestep", 1))
        if S <= 1:
            return self._image[0]

        raw = self._image[0]  # (S, Ny, Nx) for 2D scans
        mode = getattr(self, "_linestep_view_mode", "sum")

        if mode == "max":
            return np.nanmax(raw, axis=0)
        if mode == "slice":
            idx = int(getattr(self, "_linestep_view_index", 0)) % raw.shape[0]
            return raw[idx]
        return np.nansum(raw, axis=0)

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
        Called at the end of a full frame (one complete XY image) for each higher-d step (e.g. each Z).
        Updates display buffer and triggers GUI redraw once per frame.
        """
        if not self._acceptScanWorkerGeneration(generation):
            return
        if self._image_display.size == 0:
            return

        S = int(getattr(self, "_linestep", 1))
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
        # Set the boundary flag before the shared broker drains getChunk().
        # Otherwise an unread prior flag can make the new display frame drain
        # once here and then be re-flagged for a duplicate second delivery.
        self.__newFrameReady = True
        self.updateLatestFrame(True)
        self.sigNewFrame.emit()

    def getLatestFrame(self, is_save=True):
        """
        - when saving and linesteps>1, return raw stack
        - otherwise return display plane
        """
        S = int(getattr(self, "_linestep", 1))
        if is_save or S > 1:
            return self._image
        return self._image_display

    def getChunk(self):
        if not self.__newFrameReady:
            return np.empty((0, 0, 0), dtype=self.dtype)
        self.__newFrameReady = False
        return np.expand_dims(self._image_display, axis=0).copy()

    def flushBuffers(self):
        self.__newFrameReady = False

    def _renewImage(self):
        self._image = np.nan_to_num(self._image, nan=0.0)

    @property
    def shape(self):
        return self.__shape

    def setShape(self, img_dims):
        self.__shape = tuple(img_dims)


class ScanWorker(Worker):
    d2Step = Signal(np.ndarray, tuple, int)
    d3Step = Signal(int)
    acqDoneSignal = Signal(int)

    def __init__(self, manager, scanInfoDict, signalDict):
        super().__init__()
        self.__logger = initLogger(self, tryInheritParent=True)

        self._samples_read = 0
        self._manager = manager
        self._name = self._manager._name
        self._channel = self._manager._channel
        self.scanGeneration = int(
            getattr(manager, '_preparedScanGeneration', 0) or 0
        )
        self._inputTaskGeneration = None
        self._inputTaskClosed = False

        self._scan_dwell_time = scanInfoDict["dwell_time"]
        self._frac_det_dwell = max(
            1,
            int(round(self._scan_dwell_time * self._manager._detection_samplerate)),
        )
        self._frac_scan_det_rate = max(
            1,
            int(round(self._manager._detection_samplerate * scanInfoDict["scan_time_step"])),
        )
        self._rng = np.random.default_rng(self._manager._mock_random_seed)

        # img_dims contains physical scan axes only (no linestep); n_linesteps is separate.
        scan_dims = list(scanInfoDict["img_dims"])
        scan_axes = list(scanInfoDict.get("img_axes_phys", ["x", "y", "z"][:len(scan_dims)]))
        self._linestep = max(1, int(scanInfoDict.get("n_linesteps", 1)))

        # identify Y axis (default to 1)
        y_idx = scan_axes.index("y") if "y" in scan_axes else 1

        # recursion dims (no linestep axis)
        self._img_dims = scan_dims
        self._loop_dims = scan_dims.copy()

        # expand Y by linestep (Ny -> Ny*S)
        if len(self._loop_dims) > y_idx:
            self._loop_dims[y_idx] = int(self._loop_dims[y_idx] * self._linestep)

        # output dims for manager allocation: append linestep at end if >1 (keeps stack possible)
        self._output_image_dims = tuple(scan_dims + ([self._linestep] if self._linestep > 1 else []))

        # Nx for pixel binning (by convention img_dims starts with Nx,Ny,...)
        self._Nx = int(scan_dims[0]) if len(scan_dims) >= 1 else 1

        # ----------------------------
        # TTL sequence expansion
        # ----------------------------
        self._manager._ttlmultiplying = scanInfoDict.get("ttlmultiplying", self._manager._ttlmultiplying)
        if self._manager._ttlmultiplying:
            for target in signalDict["TTLCycleSignalsDict"].keys():
                if self._name == target:
                    self._seq_signal = signalDict["TTLCycleSignalsDict"][target].copy()
                    self._seq_signal = np.repeat(self._seq_signal, self._frac_scan_det_rate).astype(float)
                    self._seq_signal[self._seq_signal == 0] = np.nan
                    break

        # det samples per scan steps in different dims
        self._samples_d_scanstep = [round(samples) * self._frac_scan_det_rate for samples in scanInfoDict["scan_samples"]]
        self._samples_d2_period = round(scanInfoDict["scan_samples_d2_period"] * self._frac_scan_det_rate)
        self._samples_total = round(scanInfoDict["scan_samples_total"] * self._frac_scan_det_rate)

        self._throw_startzero = round(scanInfoDict["scan_throw_startzero"] * self._frac_scan_det_rate)
        self._scan_pads_initpos = [round(initpos) * self._frac_scan_det_rate for initpos in scanInfoDict["scan_pads_initpos"]]
        self._throw_settling = round(scanInfoDict["scan_throw_settling"] * self._frac_scan_det_rate)
        self._throw_startacc = round(scanInfoDict["scan_throw_startacc"] * self._frac_scan_det_rate)

        self._phase_delay = int(scanInfoDict["phase_delay"])
        self._smooth_axes = scanInfoDict["smooth_axes"]

        pad_initpos = self._scan_pads_initpos[0] if len(self._scan_pads_initpos) > 0 else 0
        self._throw_init_smooth = (pad_initpos + self._throw_settling + self._throw_startacc)
        self._throw_init_higher_d = False

        # start input task
        if not self._manager._simulation_mode:
            self._inputTaskGeneration = (
                self._manager._nidaqManager.startInputTask(
                    self._name, "ai", self._channel, "finite",
                    self._manager._nidaq_clock_source,
                    self._manager._detection_samplerate,
                    None, None,
                    self._samples_total, True, "ao/StartTrigger",
                )
            )

        # allocate buffers
        self._manager.initiateImage(self._output_image_dims)
        self._manager.setPixelSize(scanInfoDict["pixel_sizes"])

    def throwdata(self, datalen):
        if datalen > 0:
            if self._manager._simulation_mode:
                _ = self.randomInput(datalen)
            else:
                _ = self._manager._nidaqManager.readInputTask(
                    self._name, datalen,
                    generation=self._inputTaskGeneration,
                )
            self._samples_read += datalen

    def readdata(self, datalen):
        if self._manager._simulation_mode:
            data = self.randomInput(datalen)
        else:
            data = self._manager._nidaqManager.readInputTask(
                self._name, datalen,
                generation=self._inputTaskGeneration,
            )
        self._samples_read += datalen
        return data

    def samples_to_pixels(self, line_samples):
        arr = np.asarray(line_samples, dtype=float)
        expected = int(self._Nx) * int(self._frac_det_dwell)
        if arr.size < expected:
            arr = np.pad(arr, (0, expected - arr.size), mode="constant", constant_values=0)
        elif arr.size > expected:
            arr = arr[:expected]
        return arr.reshape(int(self._Nx), int(self._frac_det_dwell)).mean(axis=1)

    def run(self):
        try:
            self._runAcquisition()
        except Exception:
            self.__logger.exception('PMT scan worker crashed')
        finally:
            self.scanning = False
            self.acqDoneSignal.emit(int(self.scanGeneration))

    def _runAcquisition(self):
        self._pos = np.zeros(len(self._loop_dims), dtype="uint16")

        self.throwdata(self._phase_delay)
        self.throwdata(self._throw_startzero)

        if len(self._scan_pads_initpos) > 1:
            if any(np.greater(self._scan_pads_initpos[1:], self._scan_pads_initpos[0])):
                self._throw_init_higher_d = np.max(self._scan_pads_initpos[1:]) - self._scan_pads_initpos[0]
                self.throwdata(self._throw_init_higher_d)

        if len(self._loop_dims) == 2:
            self.throwdata(self._throw_init_smooth)

        self.run_loop_dx(dim=len(self._loop_dims))

    def run_loop_dx(self, dim):
        while self._pos[dim - 1] < self._loop_dims[dim - 1]:
            if dim > 2:
                if dim == 3:
                    if any(self._smooth_axes[:dim - 1]) or self._pos[dim - 1] == 0:
                        self.throwdata(self._throw_init_smooth)

                self.run_loop_dx(dim - 1)

                if dim >= 3:
                    # realign read samples
                    pos = np.copy(self._pos)
                    pos[dim - 1] += 1
                    supposed_samples_read = self._throw_startzero + np.sum(
                        np.multiply(pos, self._samples_d_scanstep[:-1])
                    )

                    if self._throw_init_higher_d:
                        supposed_samples_read += self._throw_init_higher_d

                    throwdatalen = supposed_samples_read - (self._samples_read - self._phase_delay)
                    if throwdatalen > 0:
                        self.throwdata(throwdatalen)

                    # full "frame" completed for this higher-d step
                    self.d3Step.emit(self.scanGeneration)
            else:
                self.run_loop_d2()

            self._pos[dim - 1] += 1

        self._pos[dim - 1] = 0

    def run_loop_d2(self):
        if not self.scanning:
            self.close()
            return

        # line index along expanded-Y (Ny*S)
        line_idx = int(self._pos[1])

        if self._pos[1] == self._loop_dims[1] - 1:
            if self._manager._ttlmultiplying:
                seq_start = self._samples_read - self._phase_delay
            data = self.readdata(self._samples_d_scanstep[1])
            if self._manager._ttlmultiplying:
                seq_end = self._samples_read - self._phase_delay
                ttl_seq = self._seq_signal[seq_start:seq_end]
        else:
            if self._manager._ttlmultiplying:
                seq_start = self._samples_read - self._phase_delay
            data = self.readdata(self._samples_d2_period)
            if self._manager._ttlmultiplying:
                seq_end = self._samples_read - self._phase_delay
                ttl_seq = self._seq_signal[seq_start:seq_end]

        # analog samples + offset
        data_arr = np.asarray(data, dtype=float) - float(self._manager._offset_v)
        line_samples = data_arr[:self._samples_d_scanstep[1]]

        if self._manager._ttlmultiplying:
            ttl_seq = ttl_seq[:self._samples_d_scanstep[1]]
            line_samples = np.multiply(line_samples, 1 * ttl_seq)

        pixels = self.samples_to_pixels(line_samples)

        # emit line and its insertion pos
        # IMPORTANT: keep same convention as APD: emit flipped pos[1:] (for XY, this is (line_idx,))
        self.d2Step.emit(
            pixels,
            tuple(np.flip(self._pos[1:])),
            self.scanGeneration,
        )


        # frame boundary for plain XY scan: only once after ALL expanded-Y lines
        if len(self._loop_dims) == 2 and (line_idx == self._loop_dims[1] - 1):
            self.d3Step.emit(self.scanGeneration)

    def close(self):
        if not self._manager._simulation_mode and not self._inputTaskClosed:
            self._manager._nidaqManager.inputTaskDone(
                self._name, self._inputTaskGeneration
            )
            self._inputTaskClosed = True

    def randomInput(self, datalen):
        datalen = int(datalen)
        if datalen <= 0:
            return np.empty((0,), dtype=np.float32)

        signal = self._rng.normal(
            loc=self._manager._mock_voltage_mean,
            scale=self._manager._mock_voltage_noise_std,
            size=datalen,
        )
        signal = np.clip(
            signal,
            self._manager._mock_voltage_min,
            self._manager._mock_voltage_max,
        )
        return (signal + self._manager._offset_v).astype(np.float32, copy=False)
