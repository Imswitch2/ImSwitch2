"""MMCoreHost: the single owner of one ``CMMCorePlus`` and of its circular buffer.

MMCore has *one* circular buffer for all cameras. It clears the buffer when any
sequence starts, loses frames silently unless ``stopOnOverflow`` is set (and then
ends the sequence instead, also silently), does not check that two cameras
deliver equally sized frames, and the tagged-image pop relabels every frame
with the currently selected camera (plan ``micromanager-bridge.md`` section 3,
P0 probe rows). So the buffer gets exactly one owner, this class:

* one drain thread pops with ``popNextImageAndMD`` and routes each frame by its
  metadata ``Camera`` tag into a bounded per-camera queue; camera managers pop
  only their own queue and never touch the MMCore buffer;
* the buffer is drained into those queues *before* every
  ``startSequenceAcquisition`` / ``stopSequenceAcquisition`` / ``setROI`` /
  ``clearCircularBuffer``, so nothing pending is lost on a state change;
* every sequence runs with ``stopOnOverflow=True`` and the drain thread turns
  "the sequence ended without anyone asking" into a recorded fault, which the
  next ``pop_frames`` / ``stop_stream`` raises as :class:`FramesLostError`;
* a second camera may start only with the frame shape and dtype of the one
  already streaming (MMCore does not enforce it; Micro-Manager PR #936).

Every MMCore call goes through :attr:`MMCoreHost.lock`, a re-entrant lock.
The host swallows nothing: the one place that decides to turn a startup
failure into a status is the hub manager (``core.py``).
"""
from __future__ import annotations

import collections
import difflib
import enum
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from ._frames import (
    FrameFormatError,
    dtype_for_bytes_per_pixel,
    frame_bytes,
    normalize_frame,
)

#: ``startSequenceAcquisition`` takes a C ``long`` for the image count; ``inf``
#: raises ``TypeError`` and ``2**31 - 1`` is accepted (P0 probe, plan section 3).
CONTINUOUS_SEQUENCE_IMAGES = 2 ** 31 - 1

#: Interval at which the drain thread looks at the MMCore buffer. MMCore has no
#: frame-ready callback in pymmcore, so the thread polls. The fastest rate
#: measured in P0 was 628 fps (a frame every 1.6 ms); half a millisecond keeps
#: the median age of a popped frame at about a third of that interval and costs
#: one lock round-trip per poll. The age measured in P0 with a 0.2 ms spin was
#: 0.2-0.4 ms median, so nothing here needs to be faster.
DRAIN_POLL_S = 0.0005

#: How long ``shutdown`` waits for the drain thread. One drain cycle is
#: sub-millisecond (P0), so a thread still alive after this long is wedged
#: inside MMCore; finalising must not hang the application's close for it.
DRAIN_JOIN_TIMEOUT_S = 5.0

#: Name of MMCore's own log file inside ``logDir``.
MMCORE_LOG_NAME = 'imswitch_mmcore.log'

#: The signal backend the hub forces (plan section 3: under a ``QApplication``
#: the default ``auto`` would pick the Qt signaler for every MMCore callback).
SIGNALS_BACKEND_ENV = 'PYMM_SIGNALS_BACKEND'
SIGNALS_BACKEND = 'psygnal'
SIGNALER_CLASS_NAME = 'CMMCoreSignaler'

#: Environment variable pymmcore-plus itself honours for the adapter folder.
MICROMANAGER_PATH_ENV = 'MICROMANAGER_PATH'

_PR_936 = ('Micro-Manager PR #936 ("proper support for multi-channel cameras") is still '
           'open: MMCore\'s single circular buffer needs equally sized frames')


# --------------------------------------------------------------------- errors
class MMCoreHostError(RuntimeError):
    """The host could not do what was asked; the message carries MMCore's reason."""


class MMAdaptersNotFoundError(MMCoreHostError):
    """No Micro-Manager device adapter folder could be found."""


class MMConfigFileNotFoundError(MMCoreHostError):
    """The hardware configuration file does not exist."""


class FrameShapeMismatchError(MMCoreHostError):
    """A start or ROI change would put differently sized frames in the shared buffer."""


class FramesLostError(MMCoreHostError):
    """A camera's stream lost frames (or ended without being asked to).

    ``label`` is the camera, ``fault`` the recorded :class:`StreamFault`.
    """

    def __init__(self, label: str, fault: 'StreamFault'):
        self.label = label
        self.fault = fault
        super().__init__(f"camera {label!r}: {fault.message}")


class StreamFaultKind(str, enum.Enum):
    #: MMCore ended the sequence because its buffer was full (``isBufferOverflowed``).
    OVERFLOW = 'overflow'
    #: The sequence ended and nobody asked for it, without the overflow flag.
    STOPPED_UNREQUESTED = 'stopped_unrequested'
    #: ``ImageNumber`` jumped: frames were dropped upstream of the buffer.
    GAP = 'image_number_gap'
    #: The per-camera queue was full because nobody popped (consumer side).
    CONSUMER_OVERFLOW = 'consumer_overflow'
    #: A frame's shape or dtype was not what the stream declared.
    FRAME_FORMAT = 'frame_format'
    #: The drain thread itself failed.
    DRAIN_FAILED = 'drain_failed'


@dataclass
class StreamFault:
    """What went wrong with a stream; raised once, by the call that consumes it."""

    kind: StreamFaultKind
    message: str


@dataclass(frozen=True)
class FrameRecord:
    """One routed frame: the pixels, MMCore's per-camera ``ImageNumber`` and the
    ``Camera`` tag it arrived with."""

    frame: np.ndarray
    image_number: int
    camera: str


@dataclass
class CameraStream:
    """Per-camera state owned by the host. Read it through ``host.snapshot()``;
    mutate it only through the host (every field is guarded by ``host.lock``)."""

    label: str
    shape: Optional[Tuple[int, int]] = None     # (height, width)
    dtype: Optional[np.dtype] = None
    queue: Deque[FrameRecord] = field(default_factory=collections.deque)
    queue_bytes: int = 0
    queue_budget_bytes: int = 0
    #: True from a successful start until stop is requested or MMCore ended it.
    streaming: bool = False
    #: Next ``ImageNumber`` expected; ``None`` while not streaming.
    expected_image_number: Optional[int] = None
    fault: Optional[StreamFault] = None
    frames_received: int = 0
    frames_delivered: int = 0
    frames_lost: int = 0
    frames_dropped_consumer: int = 0
    consumer_overflow_logged: bool = False


class MMCoreHost:
    """Owns one ``CMMCorePlus`` plus its circular buffer, drain thread and per-camera queues.

    All MMCore access, by the host and by any manager, happens under
    :attr:`lock` (``with host.lock:`` around ``host.core`` calls, or
    :meth:`call`). Frame shapes are numpy order ``(height, width)``.
    """

    def __init__(self, name: str, *, config_file: Optional[str] = None,
                 adapter_paths: Sequence[str] = (), circular_buffer_mb: Optional[int] = None,
                 log_dir: Optional[str] = None, adopt_existing_core: bool = False,
                 logger: Optional[logging.Logger] = None):
        if logger is None:
            from imswitch.imcommon.model import initLogger
            logger = initLogger(self, instanceName=name)
        self._logger = logger
        self.name = name
        self.config_file = config_file
        self.adapter_paths = tuple(adapter_paths)
        self.circular_buffer_mb = circular_buffer_mb
        self.log_dir = log_dir
        self.adopt_existing_core = bool(adopt_existing_core)

        self._lock = threading.RLock()
        self._core = None
        self._owns_core = True
        self._streams: Dict[str, CameraStream] = {}
        self._inline_labels: List[str] = []
        self._thread: Optional[threading.Thread] = None
        # Interruptible sleep of the drain thread (``Event.wait``), set on shutdown.
        self._stopping = threading.Event()
        self._unrouted: Dict[str, int] = {}
        # Camera that was MMCore's current one before a stream selected another (see _select_camera_locked).
        self._camera_before: Optional[str] = None
        self._frame_overhead_bytes = 0
        self.adapter_source: Optional[str] = None
        self.resolved_adapter_paths: Tuple[str, ...] = ()
        self.version_info: Optional[str] = None

    # ------------------------------------------------------------- basic access
    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def is_connected(self) -> bool:
        return self._core is not None

    @property
    def core(self):
        """The ``CMMCorePlus``. Hold :attr:`lock` around every call on it."""
        core = self._core
        if core is None:
            raise MMCoreHostError(f"Micro-Manager core {self.name!r} is not connected")
        return core

    def call(self, method: str, *args, **kwargs):
        """``core.<method>(*args, **kwargs)`` under the lock."""
        with self._lock:
            return getattr(self.core, method)(*args, **kwargs)

    # ----------------------------------------------------------------- connect
    def connect(self) -> None:
        """Construct (or adopt) the core, apply buffer / log settings, load the ``.cfg``.

        Any failure leaves the host unconnected and raises one
        :class:`MMCoreHostError` carrying the MMCore message (the original
        exception is its ``__cause__``).
        """
        with self._lock:
            if self._core is not None:
                raise MMCoreHostError(f"Micro-Manager core {self.name!r} is already connected")
            core = None
            try:
                core = self._build_core()
                self._configure_core(core)
            except MMCoreHostError:
                self._discard_core(core)
                raise
            except Exception as exc:
                self._discard_core(core)
                raise MMCoreHostError(f"{type(exc).__name__}: {exc}") from exc
            self._core = core
            self._stopping.clear()

    def _build_core(self):
        # Must precede construction: pymmcore-plus picks the signaler class when
        # the core is built, and under a QApplication `auto` would pick Qt.
        if not self.adopt_existing_core:
            os.environ[SIGNALS_BACKEND_ENV] = SIGNALS_BACKEND
        from pymmcore_plus import CMMCorePlus

        if self.adopt_existing_core:
            # The only place the process-wide core is touched: an explicit opt-in.
            # The default path below builds a private CMMCorePlus (pymmcore-plus
            # registers the first core of a process as its weak singleton anyway).
            core = CMMCorePlus.instance()
            self._owns_core = False
            self._logger.info("Adopting the process-wide CMMCorePlus (signal backend: %s)",
                              type(core.events).__name__)
            if self.adapter_paths:
                core.setDeviceAdapterSearchPaths(self._existing_dirs(self.adapter_paths))
            return core

        paths, source = self._resolve_adapter_paths()
        self.resolved_adapter_paths, self.adapter_source = tuple(paths), source
        self._logger.info("Micro-Manager adapter path(s) from %s: %s", source, list(paths))
        # mm_path is what a relative `.cfg` name resolves against.
        core = CMMCorePlus(mm_path=paths[0], adapter_paths=list(paths))
        self._owns_core = True
        backend = type(core.events).__name__
        if backend != SIGNALER_CLASS_NAME:
            raise MMCoreHostError(
                f"the Micro-Manager core got the {backend} signal backend, expected "
                f"{SIGNALER_CLASS_NAME} ({SIGNALS_BACKEND_ENV}={SIGNALS_BACKEND} must be set "
                f"before the core is constructed)")
        return core

    def _configure_core(self, core) -> None:
        self.version_info = f"{core.getAPIVersionInfo()} | {core.getVersionInfo()}"
        self._logger.info("MMCore versions: %s", self.version_info)

        if self.log_dir:
            log_dir = Path(self.log_dir).expanduser()
            log_dir.mkdir(parents=True, exist_ok=True)
            core.setPrimaryLogFile(str(log_dir / MMCORE_LOG_NAME))

        if self.circular_buffer_mb is not None:
            core.setCircularBufferMemoryFootprint(int(self.circular_buffer_mb))
            self._logger.info("MMCore circular buffer set to %s MB", self.circular_buffer_mb)
        else:
            self._logger.info("MMCore circular buffer left at MMCore's default (%s MB)",
                              core.getCircularBufferMemoryFootprint())

        if self.config_file:
            config = Path(str(self.config_file)).expanduser()
            try:
                core.loadSystemConfiguration(str(config))
            except FileNotFoundError as exc:
                raise MMConfigFileNotFoundError(
                    f"Micro-Manager configuration file not found: {exc}") from exc
            self._logger.info("Loaded Micro-Manager configuration %s", config)

        # After the config: a .cfg may switch it on, and MMCore would then open and
        # close a shutter around every snap behind the bridge's back.
        core.setAutoShutter(False)

        from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
            QUEUED_FRAME_OVERHEAD_BYTES,
        )
        self._frame_overhead_bytes = QUEUED_FRAME_OVERHEAD_BYTES

    def _resolve_adapter_paths(self) -> Tuple[List[str], str]:
        """Explicit ``adapterPaths`` > ``MICROMANAGER_PATH`` > ``find_micromanager()``."""
        if self.adapter_paths:
            return self._existing_dirs(self.adapter_paths), 'adapterPaths'
        env = os.environ.get(MICROMANAGER_PATH_ENV)
        if env:
            return self._existing_dirs([env]), MICROMANAGER_PATH_ENV
        from pymmcore_plus import find_micromanager

        found = find_micromanager()
        if not found:
            raise MMAdaptersNotFoundError(
                "no Micro-Manager device adapters found: set adapterPaths, set "
                f"{MICROMANAGER_PATH_ENV}, run `mmcore install` (Windows / Intel macOS), or "
                "`pip install mm-test-adapters` for the demo adapters")
        return [str(found)], 'find_micromanager()'

    @staticmethod
    def _existing_dirs(paths: Sequence[str]) -> List[str]:
        expanded = [str(Path(str(p)).expanduser()) for p in paths]
        missing = [p for p in expanded if not os.path.isdir(p)]
        if missing:
            raise MMAdaptersNotFoundError(f"Micro-Manager adapter folder(s) do not exist: {missing}")
        return expanded

    def _discard_core(self, core) -> None:
        """Best-effort teardown of a core that failed to configure. The original
        error is what the caller raises; this only avoids leaking loaded devices."""
        if core is None or not self._owns_core:
            return
        try:
            core.unloadAllDevices()
        except Exception as exc:                      # noqa: BLE001 - original error wins
            self._logger.warning("Unloading devices after a failed connect also failed: %s", exc)

    # ---------------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        """Stop the drain thread and every sequence, then unload the devices.

        Every step runs even if an earlier one raised; the errors are re-raised
        together at the end as one :class:`MMCoreHostError`. The host is
        unconnected afterwards either way.
        """
        with self._lock:
            if self._core is None:
                return
            thread = self._thread
            self._stopping.set()
        if thread is not None:
            thread.join(DRAIN_JOIN_TIMEOUT_S)
            if thread.is_alive():
                self._logger.error("Drain thread of %s did not stop within %.0f s", self.name,
                                   DRAIN_JOIN_TIMEOUT_S)
        errors: List[str] = []
        with self._lock:
            core = self._core
            for stream in self._streams.values():
                if stream.streaming:
                    try:
                        self._drain_locked()
                        stream.streaming = False
                        core.stopSequenceAcquisition(stream.label)
                    except Exception as exc:          # noqa: BLE001 - collected below
                        errors.append(f"stopping {stream.label!r}: {exc}")
                        self._logger.error("Stopping %s at shutdown failed: %s", stream.label, exc)
            try:
                if self._owns_core:
                    core.unloadAllDevices()
                else:
                    for label in list(self._inline_labels):
                        core.unloadDevice(label)
            except Exception as exc:                  # noqa: BLE001 - collected below
                errors.append(f"unloading devices: {exc}")
                self._logger.error("Unloading devices at shutdown failed: %s", exc)
            # The core is released even when a step failed: a reconnect must be able
            # to build a new one, and dropping the last reference makes MMCore
            # unload whatever is left. The failures are reported below.
            self._inline_labels.clear()
            for stream in self._streams.values():
                self._reset_stream_state(stream)
            self._thread = None
            self._core = None
            if errors:
                raise MMCoreHostError('; '.join(errors))

    # ----------------------------------------------------------- inline devices
    def load_device(self, label: str, adapter: str, device: str, parent: Optional[str] = None,
                    pre_init: Optional[Mapping[str, Any]] = None) -> None:
        """loadDevice -> setParentLabel -> pre-init properties -> initializeDevice.

        A device that fails part-way is unloaded again and the MMCore error re-raised.
        """
        with self._lock:
            core = self.core
            core.loadDevice(label, adapter, device)
            try:
                if parent:
                    core.setParentLabel(label, parent)
                for prop, value in (pre_init or {}).items():
                    core.setProperty(label, prop, value)
                core.initializeDevice(label)
            except Exception:
                try:
                    core.unloadDevice(label)
                except Exception as exc:              # noqa: BLE001 - the load error is re-raised
                    self._logger.warning("Unloading half-loaded device %s failed: %s", label, exc)
                raise
            self._inline_labels.append(label)

    def unload_device(self, label: str) -> None:
        with self._lock:
            stream = self._streams.get(label)
            if stream is not None:
                if stream.streaming:
                    self._stop_locked(stream)
                del self._streams[label]
            self.core.unloadDevice(label)
            if label in self._inline_labels:
                self._inline_labels.remove(label)

    def has_device(self, label: str) -> bool:
        with self._lock:
            return label in self.core.getLoadedDevices()

    def device_type(self, label: str):
        with self._lock:
            return self.core.getDeviceType(label)

    def labels_of_type(self, device_type) -> List[str]:
        with self._lock:
            return list(self.core.getLoadedDevicesOfType(device_type))

    def missing_device_message(self, label: str, device_type, type_name: str) -> str:
        """The "did you mean" error for a label the core does not have."""
        loaded = self.labels_of_type(device_type)
        near = difflib.get_close_matches(label, loaded, n=1)
        hint = f" Did you mean {near[0]!r}?" if near else ''
        return (f"Micro-Manager has no {type_name} device {label!r}.{hint} "
                f"Loaded {type_name} devices: {loaded}")

    def frame_format(self, label: str) -> Tuple[Tuple[int, int], np.dtype]:
        """``((height, width), dtype)`` camera ``label`` delivers now, from the core.

        MMCore only answers for the *current* camera and refuses to switch it
        while a sequence runs, so this temporarily selects ``label`` (and puts
        the previous selection back) and is refused while any stream runs --
        then the camera manager, which knows its own format, keeps it itself.
        Raises
        :class:`~._frames.UnsupportedPixelTypeError` for colour cameras.
        """
        with self._lock:
            core = self.core
            previous = core.getCameraDevice()
            if previous != label:
                if self.running_streams():
                    raise MMCoreHostError(
                        f"cannot read camera {label!r}'s frame format while "
                        f"{self.running_streams()} stream: MMCore only reports the current "
                        f"camera and cannot switch it during a sequence")
                core.setCameraDevice(label)
            try:
                dtype = dtype_for_bytes_per_pixel(
                    core.getBytesPerPixel(), core.getNumberOfComponents())
                return (int(core.getImageHeight()), int(core.getImageWidth())), dtype
            finally:
                if previous != label:
                    core.setCameraDevice(previous)

    def sensor_geometry(self, label: str) -> Tuple[int, int, Tuple[int, int, int, int]]:
        """``(full_width, full_height, (x, y, w, h))`` of camera ``label`` at its current binning.

        The full frame is what ``clearROI`` gives, and ``clearROI`` / ``getImageWidth``
        only act on MMCore's *current* camera, so this temporarily selects ``label``,
        clears the ROI, reads the size and puts back both the camera's own ROI (when
        it was not the full frame) and the previous camera selection. The last
        element is the ROI as it was found, in the same (binned) pixel units.

        Idle only: ``setROI`` / ``clearROI`` clear MMCore's whole circular buffer
        (plan section 3, P1a rows) and MMCore cannot switch cameras during a
        sequence, so with any stream running this raises :class:`MMCoreHostError`.
        """
        with self._lock:
            core = self.core
            if self.running_streams():
                raise MMCoreHostError(
                    f"cannot read the sensor geometry of camera {label!r} while "
                    f"{self.running_streams()} stream: clearing the ROI would clear MMCore's "
                    f"shared circular buffer and the camera selection cannot change during a sequence")
            self._drain_locked()
            previous = core.getCameraDevice()
            found = tuple(int(v) for v in core.getROI(label))
            if previous != label:
                core.setCameraDevice(label)
            try:
                core.clearROI()
                full_width, full_height = int(core.getImageWidth()), int(core.getImageHeight())
                if found != (0, 0, full_width, full_height):
                    core.setROI(label, *found)
            finally:
                if previous != label:
                    core.setCameraDevice(previous)
            return full_width, full_height, found

    # ------------------------------------------------------------ camera streams
    def register_camera(self, label: str) -> CameraStream:
        """The stream record for camera ``label`` (created on first call)."""
        from pymmcore_plus import DeviceType

        with self._lock:
            stream = self._streams.get(label)
            if stream is not None:
                return stream
            if not self.has_device(label) or self.device_type(label) != DeviceType.Camera:
                raise MMCoreHostError(self.missing_device_message(label, DeviceType.Camera, 'camera'))
            stream = CameraStream(label=label)
            self._streams[label] = stream
            return stream

    def stream(self, label: str) -> CameraStream:
        with self._lock:
            try:
                return self._streams[label]
            except KeyError:
                raise MMCoreHostError(
                    f"camera {label!r} is not registered with the host "
                    f"(registered: {sorted(self._streams)})") from None

    def running_streams(self) -> List[str]:
        """Labels whose MMCore sequence the host started and has not ended."""
        with self._lock:
            return [s.label for s in self._streams.values() if s.streaming]

    def start_stream(self, label: str, shape: Tuple[int, int], dtype) -> bool:
        """Start a continuous sequence on ``label``; True if it was started now.

        ``shape`` is ``(height, width)`` and ``dtype`` the pixel dtype the camera
        delivers now (:meth:`frame_format` reads them while nothing streams; the
        camera manager tracks them across ROI / binning / pixel-type changes).
        Raises :class:`FrameShapeMismatchError` (naming PR #936) when another
        stream runs with a different shape or dtype, and :class:`FramesLostError`
        while an earlier fault of this stream has not been consumed (by
        :meth:`stop_stream` or :meth:`acknowledge_fault`). Starting a stream
        that already runs with the same format is a no-op returning False.
        """
        with self._lock:
            stream = self.stream(label)
            shape, dtype = (int(shape[0]), int(shape[1])), np.dtype(dtype)
            if stream.streaming:
                if (stream.shape, stream.dtype) == (shape, dtype):
                    return False
                raise MMCoreHostError(
                    f"camera {label!r} already streams {stream.shape} {stream.dtype}; "
                    f"stop it before starting with {shape} {dtype}")
            if stream.fault is not None:
                raise FramesLostError(label, stream.fault)
            self._check_format_against_running(stream, shape, dtype)
            stream.shape, stream.dtype = shape, dtype
            self._reset_run_counters(stream)
            self._start_locked(stream)
            return True

    def stop_stream(self, label: str) -> None:
        """Drain, stop the sequence, then raise the stream's pending fault, if any.

        The fault is consumed by raising it; the queue keeps its frames.
        """
        with self._lock:
            stream = self.stream(label)
            self._stop_locked(stream)
            self._raise_pending_fault(stream)

    def pop_frames(self, label: str, max_frames: Optional[int] = None) -> List[np.ndarray]:
        """Frames from THIS camera's queue only, oldest first, as one equally shaped run."""
        return [record.frame for record in self.pop_records(label, max_frames)]

    def pop_records(self, label: str, max_frames: Optional[int] = None) -> List[FrameRecord]:
        """Like :meth:`pop_frames` but with ``ImageNumber`` and ``Camera`` tag.

        The list ends where the frame shape changes (after a ROI change the
        older frames keep their shape): the next call returns the next run.
        Raises :class:`FramesLostError` while a fault is pending.
        """
        with self._lock:
            stream = self.stream(label)
            if stream.fault is not None:
                raise FramesLostError(label, stream.fault)
            queue, popped = stream.queue, []
            run_shape = queue[0].frame.shape if queue else None
            while queue and (max_frames is None or len(popped) < max_frames):
                if queue[0].frame.shape != run_shape:
                    break
                record = queue.popleft()
                stream.queue_bytes -= self._cost(record.frame)
                popped.append(record)
            stream.frames_delivered += len(popped)
            return popped

    def flush_stream(self, label: str) -> None:
        """Empty THIS camera's queue. The MMCore buffer is cleared only when no stream runs."""
        with self._lock:
            stream = self.stream(label)
            self._drain_locked()
            stream.queue.clear()
            stream.queue_bytes = 0
            if not self.running_streams():
                self.core.clearCircularBuffer()

    def acknowledge_fault(self, label: str) -> Optional[StreamFault]:
        """Consume and return the stream's pending fault without raising it."""
        with self._lock:
            stream = self.stream(label)
            fault, stream.fault = stream.fault, None
            return fault

    def set_roi(self, label: str, x: int, y: int, w: int, h: int) -> Tuple[int, int, int, int]:
        """drain -> stop -> setROI -> read back -> restart if it was running.

        Returns the ROI the adapter applied ``(x, y, w, h)``; adapters snap, so
        use that rather than the request. If the size would put differently
        sized frames next to another running camera, nothing is changed
        (:class:`FrameShapeMismatchError`); if only the size the adapter snapped
        to clashes, the previous ROI is restored first.

        MMCore clears its whole circular buffer on ``setROI`` -- for *any*
        camera, even an idle one (observed: a running camera's unread frames
        vanish and its ``ImageNumber`` restarts at 0) -- so the ROI of a camera
        must only ever change through this method, which drains first.
        """
        with self._lock:
            core = self.core
            stream = self.stream(label)
            self._check_format_against_running(stream, (int(h), int(w)), stream.dtype)
            was_streaming = stream.streaming
            previous = tuple(int(v) for v in core.getROI(label))
            previous_shape = stream.shape
            self._stop_locked(stream)                  # drains first; pending frames stay queued
            try:
                self._set_roi_locked(label, (int(x), int(y), int(w), int(h)))
                applied = tuple(int(v) for v in core.getROI(label))
                shape = (applied[3], applied[2])
                self._check_format_against_running(stream, shape, stream.dtype)
                stream.shape = shape
                if was_streaming:
                    self._start_locked(stream)
            except Exception:
                # Refused request, size clash or a restart that failed: put the old
                # ROI back and the camera back to what it was doing, then report
                # the cause (a failed rollback is logged, it must not hide it).
                stream.shape = previous_shape
                try:
                    self._set_roi_locked(label, previous)
                    if was_streaming:
                        self._start_locked(stream)
                except Exception as rollback_error:   # noqa: BLE001 - the original error is raised
                    self._logger.error("Restoring ROI %s of %s failed: %s", previous, label,
                                       rollback_error)
                raise
            return applied

    def _set_roi_locked(self, label: str, roi: Tuple[int, int, int, int]) -> None:
        self._drain_locked()
        self.core.setROI(label, *roi)
        self._begin_new_epoch()

    def _begin_new_epoch(self) -> None:
        """MMCore cleared its buffer and with it the per-camera ``ImageNumber``
        counters (observed on every ``startSequenceAcquisition`` and ``setROI``):
        every running stream's next frame is number 0."""
        for stream in self._streams.values():
            if stream.streaming:
                stream.expected_image_number = 0

    def drain_now(self) -> None:
        """Move everything in the MMCore buffer into the per-camera queues."""
        with self._lock:
            self._drain_locked()

    def snapshot(self) -> Dict[str, Any]:
        """Plain-data state for status displays and tests."""
        with self._lock:
            return {
                'connected': self._core is not None,
                'adapter_source': self.adapter_source,
                'adapter_paths': list(self.resolved_adapter_paths),
                'version_info': self.version_info,
                'drain_thread_alive': bool(self._thread is not None and self._thread.is_alive()),
                'unrouted_frames': dict(self._unrouted),
                'streams': {
                    label: {
                        'streaming': s.streaming,
                        'shape': s.shape,
                        'dtype': None if s.dtype is None else str(s.dtype),
                        'queued_frames': len(s.queue),
                        'queued_bytes': s.queue_bytes,
                        'frames_received': s.frames_received,
                        'frames_delivered': s.frames_delivered,
                        'frames_lost': s.frames_lost,
                        'frames_dropped_consumer': s.frames_dropped_consumer,
                        'fault': None if s.fault is None else {
                            'kind': s.fault.kind.value, 'message': s.fault.message},
                    }
                    for label, s in self._streams.items()
                },
            }

    # ---------------------------------------------------- internals (lock held)
    def _check_format_against_running(self, stream: CameraStream, shape, dtype) -> None:
        for other in self._streams.values():
            if other is stream or not other.streaming:
                continue
            # dtype None: a camera that has not streamed yet (set_roi); the full check runs at its start.
            if other.shape != shape or (dtype is not None and other.dtype != dtype):
                raise FrameShapeMismatchError(
                    f"camera {stream.label!r} would deliver {shape} {dtype} frames while camera "
                    f"{other.label!r} is streaming {other.shape} {other.dtype}. {_PR_936}; "
                    f"MMCore would drop one camera's frames silently. Stop {other.label!r} first "
                    f"or give both cameras the same ROI, binning and pixel type.")

    def _start_locked(self, stream: CameraStream) -> None:
        """Start the sequence; the caller has checked the format."""
        self._drain_locked()                           # MMCore clears the buffer on start: nothing may be pending
        self._select_camera_locked(stream.label)
        stream.queue_budget_bytes = queue_budget_bytes()
        self.core.startSequenceAcquisition(
            stream.label, CONTINUOUS_SEQUENCE_IMAGES, 0, True)
        stream.streaming = True
        self._begin_new_epoch()
        self._ensure_drain_thread()

    def _select_camera_locked(self, label: str) -> None:
        """Make ``label`` MMCore's current camera when no stream runs.

        ``popNextImageAndMD`` wraps every buffer slot with the width, height and
        pixel size of the *current* camera (the metadata carries the frame's own,
        which is how a mismatch is noticed), so a stream of a camera that is not
        the current one and differs in size would be read past the end of its
        slot. MMCore refuses to switch cameras while a sequence runs; every
        stream that runs alongside has the same format (checked at start), so
        whichever camera was selected first stays correct for all of them.
        """
        core = self.core
        if self.running_streams():
            return
        current = core.getCameraDevice()
        if current != label:
            if self._camera_before is None:
                self._camera_before = current
            core.setCameraDevice(label)

    def _restore_camera_locked(self) -> None:
        """Put MMCore's camera selection back once the last stream stopped, so the
        bridge leaves no side effect on code that snaps with the current camera."""
        previous, self._camera_before = self._camera_before, None
        if previous is not None and not self.running_streams() and (
                previous == '' or previous in self.core.getLoadedDevices()):
            self.core.setCameraDevice(previous)

    def _stop_locked(self, stream: CameraStream) -> None:
        """Drain, stop (requested), drain what arrived meanwhile. Idempotent."""
        self._drain_locked()
        if stream.streaming:
            stream.streaming = False                   # requested: not a fault from here on
            stream.expected_image_number = None
            self.core.stopSequenceAcquisition(stream.label)
            self._drain_locked()
        else:
            # Also ends a sequence MMCore stopped by itself; no-op on an idle camera.
            self.core.stopSequenceAcquisition(stream.label)
        if not self.running_streams():
            self._restore_camera_locked()

    def _raise_pending_fault(self, stream: CameraStream) -> None:
        if stream.fault is not None:
            fault, stream.fault = stream.fault, None
            raise FramesLostError(stream.label, fault)

    @staticmethod
    def _reset_run_counters(stream: CameraStream) -> None:
        stream.frames_received = stream.frames_delivered = 0
        stream.frames_lost = stream.frames_dropped_consumer = 0
        stream.consumer_overflow_logged = False

    def _reset_stream_state(self, stream: CameraStream) -> None:
        stream.streaming = False
        stream.expected_image_number = None
        stream.fault = None
        stream.queue.clear()
        stream.queue_bytes = 0

    def _cost(self, frame: np.ndarray) -> int:
        return int(frame.nbytes) + self._frame_overhead_bytes

    def _ensure_drain_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._drain_loop, name=f'mmcore-drain-{self.name}', daemon=True)
        self._thread.start()

    def _drain_loop(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._stopping.is_set() or self._core is None or not self.running_streams():
                        if self._thread is threading.current_thread():
                            self._thread = None
                        return
                    self._drain_locked()
                self._stopping.wait(DRAIN_POLL_S)
        except Exception as exc:                      # noqa: BLE001 - recorded on every stream below
            self._logger.exception("Drain thread of %s failed", self.name)
            with self._lock:
                for stream in self._streams.values():
                    if stream.streaming and stream.fault is None:
                        stream.fault = StreamFault(
                            StreamFaultKind.DRAIN_FAILED,
                            f"the frame drain thread failed ({type(exc).__name__}: {exc}); "
                            f"frames may have been lost")
                if self._thread is threading.current_thread():
                    self._thread = None

    def _drain_locked(self) -> None:
        """Pop everything in the MMCore buffer into the per-camera queues.

        Order matters: whether each sequence still runs, and the overflow flag,
        are read *before* the pops. A sequence that is already stopped cannot add
        frames, so one pass then holds all of its frames; read afterwards, a
        frame could land between the pops and the check.
        """
        core = self._core
        if core is None:
            return
        ended = [s for s in self._streams.values()
                 if s.streaming and not core.isSequenceRunning(s.label)]
        overflowed = core.isBufferOverflowed() if ended else False
        while core.getRemainingImageCount():
            for _ in range(core.getRemainingImageCount()):
                img, md = core.popNextImageAndMD()
                self._route(img, md)
        for stream in ended:
            stream.streaming = False
            stream.expected_image_number = None
            if overflowed:
                self._record_fault(
                    stream, StreamFaultKind.OVERFLOW,
                    f"MMCore ended the sequence because its circular buffer overflowed "
                    f"(frames were produced faster than they were drained); "
                    f"{stream.frames_received} frame(s) were delivered before the end and an "
                    f"unknown number after it were lost")
            else:
                self._record_fault(
                    stream, StreamFaultKind.STOPPED_UNREQUESTED,
                    f"the sequence ended without being stopped "
                    f"({stream.frames_received} frame(s) delivered)")

    def _record_fault(self, stream: CameraStream, kind: StreamFaultKind, message: str) -> None:
        self._logger.error("Camera %s: %s", stream.label, message)
        if stream.fault is None:                       # the first cause stays the reported one
            stream.fault = StreamFault(kind, message)

    def _route(self, img, md) -> None:
        """Put one popped frame into the queue of the camera its metadata names."""
        label = md['Camera']
        stream = self._streams.get(label)
        if stream is None:
            if label not in self._unrouted:
                self._logger.error("Frame from camera %r, which is not registered with the host; "
                                   "dropping it (counted in snapshot()['unrouted_frames'])", label)
            self._unrouted[label] = self._unrouted.get(label, 0) + 1
            return
        try:
            frame = normalize_frame(img, md)
            if stream.shape is not None and (frame.shape, frame.dtype) != (stream.shape, stream.dtype):
                raise FrameFormatError(
                    f"frame is {frame.shape} {frame.dtype} but the stream was started as "
                    f"{stream.shape} {stream.dtype}")
        except FrameFormatError as exc:
            stream.frames_lost += 1
            self._record_fault(stream, StreamFaultKind.FRAME_FORMAT, f"unusable frame: {exc}")
            return
        number = int(md['ImageNumber'])
        expected = stream.expected_image_number
        if expected is not None and number != expected:
            missing = max(0, number - expected)
            stream.frames_lost += missing
            self._record_fault(
                stream, StreamFaultKind.GAP,
                f"ImageNumber {number} arrived where {expected} was expected: {missing} frame(s) "
                f"were dropped before the circular buffer"
                if missing else
                f"ImageNumber {number} arrived where {expected} was expected: the camera's "
                f"counter went backwards (the circular buffer was cleared behind the host's back)")
        stream.expected_image_number = number + 1
        stream.frames_received += 1
        self._enqueue(stream, FrameRecord(frame, number, label))

    def _enqueue(self, stream: CameraStream, record: FrameRecord) -> None:
        """Bounded by the per-detector queue budget, in bytes, like ``DetectorManager``.

        DetectorManager declares a consumer's stream incomplete when its queue
        would exceed ``_queueBudgetBytes()`` (it drops the backlog and fails the
        consumer's next read), and admits one delivery to an empty queue however
        large. Same here: the budget and the per-frame cost are DetectorManager's,
        the frame that does not fit evicts the *oldest* one (a deque makes that
        O(1), where the whole-queue drop only saves it moving a list), and the
        loss is a fault, so a consumer that stopped popping fails instead of
        recording a stream with a hole in it.
        """
        cost = self._cost(record.frame)
        queue = stream.queue
        while queue and stream.queue_bytes + cost > stream.queue_budget_bytes:
            evicted = queue.popleft()
            stream.queue_bytes -= self._cost(evicted.frame)
            stream.frames_dropped_consumer += 1
            if not stream.consumer_overflow_logged:
                stream.consumer_overflow_logged = True
                self._record_fault(
                    stream, StreamFaultKind.CONSUMER_OVERFLOW,
                    f"the frame queue exceeded its {stream.queue_budget_bytes} byte budget "
                    f"(memory.perDetectorQueueMB in imcontrol_options.json) because frames "
                    f"were not read; the oldest frames are dropped")
        queue.append(record)
        stream.queue_bytes += cost


def queue_budget_bytes() -> int:
    """The per-detector consumer-queue budget in force (``DetectorManager``'s)."""
    from imswitch.imcontrol.model.managers.detectors.DetectorManager import _queueBudgetBytes
    return _queueBudgetBytes()
