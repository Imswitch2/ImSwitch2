"""MicroManagerCameraManager: one Micro-Manager Camera device as an ImSwitch2 detector.

Built on :class:`~._core.MMCoreHost` (the single owner of MMCore's circular
buffer) and the hub :class:`~.core.MicroManagerCoreManager`; see plan
``micromanager-bridge.md`` sections 4.3 to 4.5 and 4.7. Mono cameras only.

Manager properties (``schemas/micromanager_camera.schema.json``):

- ``rs232device`` -- name of the ``micromanager.core`` hub in ``rs232devices``
- ``label`` -- the Micro-Manager device label
- ``adapter`` + ``device`` (+ ``parent``, ``preInitProperties``) -- *inline
  mode*: the manager loads the device itself and unloads it on finalize;
  without them the label must already be loaded by the hub's ``.cfg``
  (*system-config mode*)
- ``cameraPixelSizeUm`` -- sample-plane pixel size (wins over Micro-Manager's)

Frame path. The host drains MMCore's buffer into a per-camera queue; this
manager only ever pops *its own* queue and never touches the buffer. Two
readers want those frames:

- ``getChunk`` (recording and every ``readChunk`` consumer, through the base
  class's broker) must see every frame once, in order;
- ``getLatestFrame`` (live view) wants the newest one, and must keep working
  while a recording drains the chunk path (the TIS lesson: live view read the
  same deque a recording emptied and went black).

Resolution: ``getChunk`` and ``getLatestFrame`` share one *staging* list in
this manager. Whichever pulls from the host puts the frames it took into the
list; ``getChunk`` returns and clears the list, ``getLatestFrame`` returns the
newest frame it has ever seen (``_latest``) and so never depends on what a
consumer emptied. With a registered ``readChunk`` consumer, ``getLatestFrame``
stages what it pulls, so a direct call cannot steal frames from the recording
(the broker itself uses ``getChunk`` + its own latch for live view then, see
``DetectorManager.getLatestFrameShared``). With no registered consumer nobody
could ever read staged frames, so ``getLatestFrame`` keeps only the newest and
drops the rest: this is what keeps a plain live view from filling the host's
queue (which would be a consumer-overflow fault) or from hoarding memory.
"""
from __future__ import annotations

import collections
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
    CAMERA_PIXEL_SIZE_PARAM,
    CAMERA_PIXEL_SIZE_SOURCE_PARAM,
    PIXEL_SIZE_SOURCE_DEFAULT,
    QUEUED_FRAME_OVERHEAD_BYTES,
    RawFrameUnavailableError,
    configuredCameraPixelSize,
)
from imswitch.pluginapi import DetectorManager, DetectorNumberParameter

from . import _properties
from ._bridge import MicroManagerDeviceMixin
from ._core import FramesLostError, MMCoreHostError, StreamFault, StreamFaultKind, queue_budget_bytes
from ._frames import UnsupportedPixelTypeError

#: ``fullShape`` ``(width, height)`` of a camera that was built while its hub was
#: not connected. DetectorManager needs a shape at construction and the sensor
#: size is only known once Micro-Manager has the device; (1, 1) is the smallest
#: valid frame and obviously not a real sensor. The real size replaces it on
#: reconnect (``_setFullShape``).
ABSENT_FULL_SHAPE = (1, 1)

#: ``model`` of a camera whose hub is not connected (replaced on reconnect).
ABSENT_MODEL = 'Micro-Manager camera (not connected)'

#: MMCore property names the manager maps to first-class ImSwitch2 concepts.
PROPERTY_EXPOSURE = 'Exposure'
PROPERTY_BINNING = 'Binning'

#: Parameter that carries the exposure, and where it sits in the settings tree.
PARAM_EXPOSURE = 'Exposure'
EXPOSURE_GROUP = 'Timings'
#: Micro-Manager's exposure unit (``setExposure`` / ``getExposure``).
EXPOSURE_UNITS = 'ms'

#: Group and unit of the pixel-size parameter (same as ``makeCameraPixelSizeParameter``).
PIXEL_SIZE_GROUP = 'Miscellaneous'
PIXEL_SIZE_UNITS = 'µm'

#: Provenance of a pixel size taken from Micro-Manager's pixel-size configuration
#: (``DetectorManager`` only knows setup file / assumed default / user).
PIXEL_SIZE_SOURCE_MICROMANAGER = 'Micro-Manager pixel-size config'

#: Mono-only refusal: the plan's reason (section 4.4), shown with the camera label.
_COLOUR_REFUSAL = (
    "Micro-Manager camera {label!r} delivers colour frames ({reason}). The Micro-Manager "
    "bridge carries mono cameras only: ImSwitch2's chunk contract is (numFrames, height, "
    "width), and the Zarr writer and napari layers assume it; a per-component split is a "
    "later feature. Give the camera an 8-bit or 16-bit mono PixelType (a pre-init property "
    "or the .cfg).")


@dataclass
class _Identity:
    """Everything read from Micro-Manager when the device is adopted."""

    model: str
    full_shape: Tuple[int, int]               # (width, height) at the current binning
    roi: Tuple[int, int, int, int]            # (x, y, w, h) as applied
    stream_hw: Tuple[int, int]                # (height, width) of a frame
    dtype: np.dtype
    binning: int
    binning_values: Dict[int, Optional[str]]  # binning -> the allowed-value text MMCore wants
    exposure: float
    properties: Dict[str, Any]                # mapped MM properties (parameters)
    pixel_size_um: Optional[float]            # unbinned, from MM's pixel-size config; None = unknown


class MicroManagerCameraManager(MicroManagerDeviceMixin, DetectorManager):
    """A Micro-Manager Camera device as a detector (mono only)."""

    BRIDGE_MANAGER_ID = 'micromanager.camera'
    BRIDGE_CATEGORY = 'detector'
    BRIDGE_DEVICE_NOUN = 'camera'
    BRIDGE_DEVICE_TYPES = ('Camera',)
    BRIDGE_TYPE_NOUN = 'camera'

    def __init__(self, detectorInfo, name, **lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        properties = dict(detectorInfo.managerProperties or {})
        self._bridge_identify(properties, name, self.__logger)
        self._bridge_attach(properties, lowLevelManagers)

        # Manager-local state. Everything that touches frames or MMCore is guarded by
        # ``host.lock`` (one lock, so there is no lock order to get wrong).
        self._ready = False                       # False until the device was adopted (and during DetectorManager.__init__)
        self._staged: Deque[np.ndarray] = collections.deque()
        self._staged_bytes = 0
        self._staged_dropped = 0
        self._staged_fault: Optional[StreamFault] = None   # staged frames evicted: raised once by getChunk/stopAcquisition
        self._latest: Optional[np.ndarray] = None
        self._lost_reported = None                # StreamFault already logged by the live-view path
        self._stream_hw: Tuple[int, int] = (ABSENT_FULL_SHAPE[1], ABSENT_FULL_SHAPE[0])
        self._stream_dtype: Optional[np.dtype] = None
        self._binnings: List[int] = [1]           # supportedBinnings, mutated in place
        self._binning_values: Dict[int, Optional[str]] = {1: None}
        self._mm_names: Tuple[str, ...] = ()      # parameter names that are Micro-Manager properties
        self._edited: Dict[str, str] = {}         # property -> text written by this session (re-applied on reconnect)
        self._mm_pixel_size = False               # the pixel size came from Micro-Manager
        self._resume = False                      # a stream was running when the transport went down

        # Standard status + reconnect lifecycle; adoption (label, geometry, parameters) follows.
        self._bridge_install_backend(detectorInfo)
        identity: Optional[_Identity] = None
        try:
            identity = self._bridge_adopt(self._adopt_label)

            parameters: Dict[str, Any] = {}
            pixel_param = self._initial_pixel_size_parameter(detectorInfo, identity)
            if identity is not None:
                parameters[PARAM_EXPOSURE] = self._exposure_parameter(identity.exposure)
                parameters.update(identity.properties)
                self._binnings[:] = sorted(identity.binning_values)
            parameters[CAMERA_PIXEL_SIZE_PARAM] = pixel_param
            super().__init__(
                detectorInfo, name,
                fullShape=identity.full_shape if identity else ABSENT_FULL_SHAPE,
                supportedBinnings=self._binnings,
                model=identity.model if identity else ABSENT_MODEL,
                parameters=parameters, croppable=True)
        except BaseException:
            self._unload_inline()            # a setup error must not leave the device loaded in the core
            raise

        if identity is not None:
            self._finish_identity(identity)
            self._mark_pixel_size_source()
            self._setConnected(f'{self.model}: {self._label} connected')

    # ------------------------------------------------------------------ setup
    def _adopt_label(self) -> _Identity:
        """Resolve the label, register it with the host and read everything from Micro-Manager."""
        with self._host.lock:
            self._resolve_label()
            self._host.register_camera(self._label)
            return self._read_identity()

    def _read_identity(self) -> _Identity:
        host, label = self._host, self._label
        with host.lock:
            core = host.core
            try:
                stream_hw, dtype = host.frame_format(label)
            except UnsupportedPixelTypeError as exc:
                raise UnsupportedPixelTypeError(_COLOUR_REFUSAL.format(label=label, reason=exc)) from exc
            full_width, full_height, roi = host.sensor_geometry(label)
            binning_values, binning = self._read_binning(core)
            return _Identity(
                model=f'{core.getDeviceLibrary(label)}/{core.getDeviceName(label)}',
                full_shape=(full_width, full_height), roi=roi, stream_hw=stream_hw, dtype=dtype,
                binning=binning, binning_values=binning_values, exposure=float(core.getExposure(label)),
                properties=_properties.build_parameters(
                    core, label, exclude=(PROPERTY_EXPOSURE, PROPERTY_BINNING, *self._reserved_names())),
                pixel_size_um=self._micromanager_pixel_size(core))

    @staticmethod
    def _reserved_names() -> Tuple[str, ...]:
        """Parameter names the manager owns; an MM property of that name is not mapped."""
        return (PARAM_EXPOSURE, CAMERA_PIXEL_SIZE_PARAM, CAMERA_PIXEL_SIZE_SOURCE_PARAM)

    def _read_binning(self, core) -> Tuple[Dict[int, Optional[str]], int]:
        """``({binning: allowed-value text}, current binning)`` from the ``Binning`` property."""
        label = self._label
        if PROPERTY_BINNING not in core.getDevicePropertyNames(label):
            return {1: None}, 1
        values: Dict[int, Optional[str]] = {}
        for text in core.getAllowedPropertyValues(label, PROPERTY_BINNING):
            try:
                values[int(text)] = str(text)
            except ValueError:
                self.__logger.warning(
                    'Binning value %r of %s is not a whole number; it is not offered', text, label)
        try:
            current = int(core.getProperty(label, PROPERTY_BINNING))
        except ValueError:
            current = 1
            self.__logger.warning('Binning of %s reads %r, not a whole number; assuming 1',
                                  label, core.getProperty(label, PROPERTY_BINNING))
        values.setdefault(current, str(current))
        return values, current

    def _micromanager_pixel_size(self, core) -> Optional[float]:
        """Unbinned sample-plane pixel size from Micro-Manager's pixel-size config, else ``None``.

        ``getPixelSizeUm()`` multiplies by the binning of MMCore's *current* camera (P0
        probe) and is 0.0 when no pixel-size config matches; divide by that binning to
        get the size ImSwitch2 expects, and treat 0.0 as "not configured".
        """
        size = float(core.getPixelSizeUm())
        if size <= 0.0:
            return None
        current = core.getCameraDevice()
        binning = 1
        if current and PROPERTY_BINNING in core.getDevicePropertyNames(current):
            try:
                binning = int(core.getProperty(current, PROPERTY_BINNING))
            except ValueError:
                binning = 1
        return size / max(1, binning)

    @staticmethod
    def _exposure_parameter(value: float) -> DetectorNumberParameter:
        return DetectorNumberParameter(
            group=EXPOSURE_GROUP, value=float(value), valueUnits=EXPOSURE_UNITS, editable=True)

    def _initial_pixel_size_parameter(self, detectorInfo, identity: Optional[_Identity]) -> DetectorNumberParameter:
        """Pixel-size rule of plan 4.4: setup file > Micro-Manager's config > placeholder."""
        declared = configuredCameraPixelSize(detectorInfo.managerProperties) is not None
        if declared:
            self.__logger.info('Camera pixel size: from the setup file (cameraPixelSizeUm)')
        elif identity is not None and identity.pixel_size_um is not None:
            self._mm_pixel_size = True
            self.__logger.info(
                'Camera pixel size %.4g um: from Micro-Manager\'s pixel-size config (binning divided out); '
                'cameraPixelSizeUm is not set in the setup file', identity.pixel_size_um)
            return DetectorNumberParameter(
                group=PIXEL_SIZE_GROUP, value=identity.pixel_size_um, valueUnits=PIXEL_SIZE_UNITS, editable=True)
        else:
            self.__logger.info('Camera pixel size: neither the setup file nor Micro-Manager has one; '
                               'the placeholder default applies')
        return DetectorManager.makeCameraPixelSizeParameter(detectorInfo)

    def _mark_pixel_size_source(self) -> None:
        """Say in the provenance parameter that the value came from Micro-Manager."""
        if not self._mm_pixel_size:
            return
        source = self.parameters[CAMERA_PIXEL_SIZE_SOURCE_PARAM]
        if PIXEL_SIZE_SOURCE_MICROMANAGER not in source.options:
            source.options.append(PIXEL_SIZE_SOURCE_MICROMANAGER)
        source.value = PIXEL_SIZE_SOURCE_MICROMANAGER

    def _finish_identity(self, identity: _Identity) -> None:
        """Store what was read: geometry, format, binning and which parameters are MM properties."""
        self._frameStart = (identity.roi[0], identity.roi[1])
        self._shape = (identity.roi[2], identity.roi[3])
        self._stream_hw, self._stream_dtype = identity.stream_hw, np.dtype(identity.dtype)
        self._binning_values = dict(identity.binning_values)
        self._binning = identity.binning
        self._mm_names = tuple(identity.properties)
        self._ready = True

    # ------------------------------------------------------------- properties
    @property
    def configOwnedParameters(self) -> frozenset:
        """Also the pixel size when Micro-Manager supplied it: a saved widget state must not
        reinstate an old value over the configuration that is authoritative at every start."""
        owned = super().configOwnedParameters
        return owned | {CAMERA_PIXEL_SIZE_PARAM} if self._mm_pixel_size else owned

    @property
    def dtype(self) -> np.dtype:
        """The dtype the camera delivers now (``PixelType``), known before the first frame."""
        cached = self.__dict__.get('_stream_dtype')
        return cached if cached is not None else super().dtype

    def _stream_format(self) -> Tuple[Tuple[int, int], np.dtype]:
        return self._stream_hw, self._stream_dtype

    # ------------------------------------------------------------- parameters
    def setParameter(self, name, value):
        if name == PARAM_EXPOSURE:
            self._requireConnected('setParameter')
            with self._host.lock:
                self._host.call('setExposure', self._label, float(value))
                applied = float(self._host.call('getExposure', self._label))
            self._warn_if_changed(name, value, applied)
            return super().setParameter(name, applied)
        if name in self._mm_names:
            self._requireConnected('setParameter')
            return super().setParameter(name, self._set_property(name, value))
        return super().setParameter(name, value)

    def _set_property(self, name, value):
        """Write one Micro-Manager property (stopped around the write if streaming); the value read back."""
        host, label = self._host, self._label
        text = _properties.coerce_for_mm(self.parameters[name], value)

        def action():
            with host.lock:
                core = host.core
                previous = core.getProperty(label, name)
                core.setProperty(label, name, text)          # MMCore's refusal propagates unchanged
                try:
                    self._sync_geometry()
                except UnsupportedPixelTypeError as exc:
                    core.setProperty(label, name, previous)  # the bridge cannot carry this: undo it
                    raise UnsupportedPixelTypeError(_COLOUR_REFUSAL.format(label=label, reason=exc)) from exc
                return _properties.read_back(core, label, name)

        applied = self._safe_camera_action(action)
        self._edited[name] = str(applied)
        self.refreshParameters()
        self._warn_if_changed(name, value, applied)
        return applied

    def _warn_if_changed(self, name, requested, applied) -> None:
        try:
            same = float(requested) == float(applied)
        except (TypeError, ValueError):
            same = str(requested) == str(applied)
        if not same:
            self.__logger.warning('%s: asked for %r, the camera took %r', name, requested, applied)

    def refreshParameters(self) -> None:
        """Re-read every mapped Micro-Manager property and the exposure from the device."""
        self._requireConnected('refreshParameters')
        with self._host.lock:
            core = self._host.core
            _properties.refresh_parameters(core, self._label, self.parameters, self._mm_names)
            self.parameters[PARAM_EXPOSURE].value = float(core.getExposure(self._label))

    def setBinning(self, binning):
        if not self.__dict__.get('_ready'):
            return super().setBinning(binning)         # DetectorManager.__init__ sets the first supported binning
        binning = int(binning)
        if binning not in self.supportedBinnings:
            raise ValueError(f'Specified binning value "{binning}" not supported by the detector')
        self._requireConnected('setBinning')
        if binning == self._binning:
            return
        host, label = self._host, self._label
        running = [other for other in host.running_streams() if other != label]
        if running:
            raise MMCoreHostError(
                f"cannot change the binning of camera {label!r} while {running} stream: the shared "
                f"circular buffer needs equally sized frames")
        text = self._binning_values[binning]
        if text is None:
            raise ValueError(f'camera {label!r} has no {PROPERTY_BINNING} property; only binning 1 exists')

        def action():
            with host.lock:
                core = host.core
                previous = core.getProperty(label, PROPERTY_BINNING)
                core.setProperty(label, PROPERTY_BINNING, text)
                try:
                    full_width, full_height, roi = host.sensor_geometry(label)
                    self._setFullShape((full_width, full_height))
                    if not self._roi_fits(roi, full_width, full_height):
                        self.__logger.warning(
                            'Binning %d left the ROI %s outside the %dx%d frame; using the full frame',
                            binning, roi, full_width, full_height)
                        host.set_roi(label, 0, 0, full_width, full_height)
                    self._sync_geometry()
                except Exception:
                    core.setProperty(label, PROPERTY_BINNING, previous)
                    raise

        self._safe_camera_action(action)
        super().setBinning(binning)
        self.refreshParameters()

    @staticmethod
    def _roi_fits(roi, width, height) -> bool:
        x, y, w, h = roi
        return w > 0 and h > 0 and x >= 0 and y >= 0 and x + w <= width and y + h <= height

    # --------------------------------------------------- geometry and format
    def _sync_geometry(self) -> bool:
        """Re-read ROI and frame format from MMCore into the manager; True if anything changed.

        Called with the stream stopped (or the camera idle). A change of frame size
        also re-reads the full frame when no other camera streams (``sensor_geometry``
        is idle-only). Raises :class:`UnsupportedPixelTypeError` if the camera now
        delivers colour.
        """
        host, label = self._host, self._label
        with host.lock:
            before = (self._frameStart, self._shape, self._stream_hw, self._stream_dtype)
            x, y, w, h = (int(v) for v in host.call('getROI', label))
            try:
                stream_hw, dtype = host.frame_format(label)
            except UnsupportedPixelTypeError:
                raise
            except MMCoreHostError:
                # Another camera streams: MMCore cannot switch the current camera, so the dtype
                # cannot be read. The frame size follows the ROI; the dtype stays as known and
                # the host checks every frame against it.
                stream_hw, dtype = (h, w), self._stream_dtype
            self._frameStart, self._shape = (x, y), (w, h)
            self._stream_hw, self._stream_dtype = stream_hw, np.dtype(dtype)
            changed = before != (self._frameStart, self._shape, self._stream_hw, self._stream_dtype)
            if changed:
                self._latest = None                       # the old frame no longer matches the new geometry
                if not host.running_streams():
                    full_width, full_height, _ = host.sensor_geometry(label)
                    self._setFullShape((full_width, full_height))
            return changed

    def _safe_camera_action(self, action):
        """stop the stream -> ``action()`` -> restart it, for settings MMCore may refuse mid-sequence.

        The format is re-read by ``action`` (via :meth:`_sync_geometry`) before the restart,
        so a changed frame size or pixel type is what the new sequence is started with. The
        action's own error is the one raised; a stop that raises (:class:`FramesLostError`)
        aborts before anything is changed.
        """
        host, label = self._host, self._label
        with host.lock:
            stream = host.register_camera(label)
            was_streaming = stream.streaming
            if was_streaming:
                host.stop_stream(label)
            error: Optional[BaseException] = None
            result = None
            try:
                result = action()
            except Exception as exc:                      # noqa: BLE001 - re-raised below, after the restart
                error = exc
            if was_streaming:
                try:
                    host.start_stream(label, *self._stream_format())
                except Exception as exc:                  # noqa: BLE001
                    if error is None:
                        error = exc
                    else:
                        self.__logger.error('Restarting %s after a failed setting also failed: %s', label, exc)
            if error is not None:
                raise error
            return result

    def crop(self, hpos, vpos, hsize, vsize):
        """Set the ROI through the host (drain -> stop -> setROI -> read back -> restart).

        Arguments and the stored ``frameStart`` / ``shape`` are in Micro-Manager's (binned)
        pixels, x/y/width/height. The values stored are the ones the adapter *applied*:
        adapters snap.
        """
        self._requireConnected('crop')
        requested = (int(hpos), int(vpos), int(hsize), int(vsize))
        host = self._host
        with host.lock:
            host.register_camera(self._label)
            applied = host.set_roi(self._label, *requested)
            x, y, w, h = applied
            self._frameStart, self._shape = (x, y), (w, h)
            self._stream_hw = (h, w)                      # the host takes (height, width)
            self._latest = None
        if applied != requested:
            self.__logger.warning(
                'Camera %s snapped the requested ROI (x0=%d, y0=%d, width=%d, height=%d) to '
                '(x0=%d, y0=%d, width=%d, height=%d); using the applied values',
                self._label, *requested, *applied)

    # ----------------------------------------------------------- acquisition
    def startAcquisition(self):
        self._requireConnected('startAcquisition')
        host = self._host
        with host.lock:
            host.register_camera(self._label)
            if not host.running_streams():
                self._sync_geometry()                     # pick up changes made behind the manager's back
            host.start_stream(self._label, *self._stream_format())

    def stopAcquisition(self):
        """Stop the stream; MMCore errors and :class:`FramesLostError` propagate (the lease
        table turns them into the hardware fault). With the hub down nothing runs any more."""
        if not (self._ready and self._host.is_connected):
            return
        self._host.stop_stream(self._label)
        self._raise_staged_fault()

    def flushBuffers(self):
        if not self._ready:
            return
        with self._host.lock:
            self._host.flush_stream(self._label)
            self._staged.clear()
            self._staged_bytes = 0
            self._staged_dropped = 0
            self._staged_fault = None

    def getChunk(self):
        """Frames since the last call, a list of equally shaped ``(h, w)`` arrays (maybe empty).

        Ends where the shape or dtype changes; the next call returns the next run. Raises
        :class:`FramesLostError` while the stream has an unconsumed fault, so a recording
        that lost frames fails instead of finishing short.
        """
        if not self._ready:
            return []
        with self._host.lock:
            self._ingest(self._host.pop_frames(self._label), stage=True)
            self._raise_staged_fault()
            return self._take_run()

    def getLatestFrame(self, is_save=False):
        """The newest frame this manager has seen, without taking frames from ``getChunk``.

        See the module docstring for the staging design. Before the first frame a live view
        gets a black frame of the right shape; a *save* (``is_save``) gets
        :class:`RawFrameUnavailableError` rather than a fabricated frame.
        """
        self._requireConnected('getLatestFrame')
        with self._host.lock:
            try:
                frames = self._host.pop_frames(self._label)
            except FramesLostError as exc:
                if self._lost_reported is not exc.fault:
                    self._lost_reported = exc.fault
                    self.__logger.error('Live view: %s (stopAcquisition raises it)', exc)
                frames = []
            self._ingest(frames, stage=bool(self._chunkConsumers))
            if self._latest is not None:
                return self._latest
            if is_save:
                raise RawFrameUnavailableError(f'{self._setupName}: no frame has arrived yet, nothing to save')
            return np.zeros(self._stream_hw, dtype=self._stream_dtype)

    def _ingest(self, frames: List[np.ndarray], *, stage: bool) -> None:
        """Remember the newest frame; keep the others for ``getChunk`` only if ``stage``. Lock held."""
        if not frames:
            return
        self._latest = frames[-1]
        if not stage:
            return
        self._staged.extend(frames)
        self._staged_bytes += sum(int(frame.nbytes) + QUEUED_FRAME_OVERHEAD_BYTES for frame in frames)
        budget = queue_budget_bytes()
        while self._staged and self._staged_bytes > budget:
            evicted = self._staged.popleft()
            self._staged_bytes -= int(evicted.nbytes) + QUEUED_FRAME_OVERHEAD_BYTES
            self._staged_dropped += 1
            if self._staged_fault is None:
                # Same rule as the host's consumer_overflow fault (plan section 4.5, "loss is an
                # error"): a recording whose frames were evicted here must fail, not finish short.
                self._staged_fault = StreamFault(
                    StreamFaultKind.CONSUMER_OVERFLOW,
                    f'frames staged for getChunk exceeded the {budget} byte queue budget '
                    f'(memory.perDetectorQueueMB in imcontrol_options.json) because nobody read '
                    f'them; the oldest were dropped')
                self.__logger.error('Camera %s: %s', self._label, self._staged_fault.message)

    def _raise_staged_fault(self) -> None:
        """Raise (once) the fault recorded when staged frames were evicted."""
        fault, self._staged_fault = self._staged_fault, None
        if fault is not None:
            raise FramesLostError(self._label, fault)

    def _take_run(self) -> List[np.ndarray]:
        """Pop the staged frames that share the first frame's shape and dtype. Lock held."""
        if not self._staged:
            return []
        first = self._staged[0]
        run = []
        while self._staged and self._staged[0].shape == first.shape and self._staged[0].dtype == first.dtype:
            frame = self._staged.popleft()
            self._staged_bytes -= int(frame.nbytes) + QUEUED_FRAME_OVERHEAD_BYTES
            run.append(frame)
        return run

    # --------------------------------------------------------------- lifecycle
    def _lifecycleSafeState(self, *, verified: bool):
        """Stop the stream before the hub goes away; remember that it was running."""
        errors: List[str] = []
        host = self._host
        if not (self._ready and host.is_connected):
            return errors
        try:
            running = host.stream(self._label).streaming
            if running:
                host.stop_stream(self._label)
        except Exception as exc:                          # noqa: BLE001 - reported to the lifecycle
            errors.append(f'could not stop the stream: {exc}')
            running = False
        self._resume = running if not verified else False
        return errors

    def _reconnected_summary(self) -> str:
        return f'{self.model}: {self._label} reconnected'

    def _onTransportReconnected(self, real: bool):
        """After the hub reloaded the core (plumbing in the mixin): restart the stream that was running."""
        resume = self._resume or self.acquisitionLeased
        self._resume = False
        errors = super()._onTransportReconnected(real)
        if errors or not real or not resume:
            return errors
        try:
            self.startAcquisition()
        except Exception as exc:                          # noqa: BLE001
            errors.append(f'could not restart the stream: {exc}')
            self._setConnectionError(errors[-1], summary=f'{self._label}: reconnected but not usable')
        return errors

    def _reinitialise(self) -> List[str]:
        """Put the device in the state this manager reports; the errors found.

        First connection (the camera was built while the hub was down): adopt what
        Micro-Manager has. Afterwards: re-apply the session's property edits, binning, ROI
        and exposure on the freshly loaded device, then check that the frame format and
        size are what the manager reports.
        """
        host = self._host
        if not self._ready:
            identity = self._adopt_label()
            self._install_late_identity(identity)
            return []
        errors: List[str] = []
        with host.lock:
            self._resolve_label()
            host.register_camera(self._label)
            core = host.core
            label = self._label
            expected_hw, expected_dtype = self._stream_hw, self._stream_dtype
            expected_roi = (*self._frameStart, *self._shape)
            expected_full = self.fullShape
            self._host.acknowledge_fault(label)
            for name, text in self._edited.items():
                try:
                    core.setProperty(label, name, text)
                except Exception as exc:                  # noqa: BLE001
                    errors.append(f'{name}: {exc}')
            text = self._binning_values.get(self._binning)
            if text is not None:
                try:
                    core.setProperty(label, PROPERTY_BINNING, text)
                except Exception as exc:                  # noqa: BLE001
                    errors.append(f'{PROPERTY_BINNING}: {exc}')
            try:
                full_width, full_height, _ = host.sensor_geometry(label)
                self._setFullShape((full_width, full_height))
                if (full_width, full_height) != tuple(expected_full):
                    errors.append(f'the sensor is now {full_width}x{full_height}, was {expected_full[0]}x{expected_full[1]}')
                if expected_roi != (0, 0, full_width, full_height):
                    applied = host.set_roi(label, *expected_roi)
                    if applied != expected_roi:
                        errors.append(f'ROI {expected_roi} came back as {applied}')
                self._sync_geometry()
            except Exception as exc:                      # noqa: BLE001
                errors.append(f'geometry: {exc}')
            try:
                core.setExposure(label, float(self.parameters[PARAM_EXPOSURE].value))
            except Exception as exc:                      # noqa: BLE001
                errors.append(f'{PARAM_EXPOSURE}: {exc}')
            if (self._stream_hw, np.dtype(self._stream_dtype)) != (expected_hw, np.dtype(expected_dtype)):
                errors.append(f'the frame format is now {self._stream_hw} {self._stream_dtype}, '
                              f'was {expected_hw} {expected_dtype}')
            try:
                self.refreshParameters()
            except Exception as exc:                      # noqa: BLE001
                errors.append(f'parameters: {exc}')
        return errors

    def _install_late_identity(self, identity: _Identity) -> None:
        """A camera built while its hub was down gets its real geometry and parameters."""
        self._setModel(identity.model)
        self._setFullShape(identity.full_shape)
        self._binnings[:] = sorted(identity.binning_values)
        existing = dict(self.parameters)
        new = {PARAM_EXPOSURE: self._exposure_parameter(identity.exposure), **identity.properties}
        self.parameters.clear()
        self.parameters.update({**new, **existing})
        source = self.parameters.get(CAMERA_PIXEL_SIZE_SOURCE_PARAM)
        if (identity.pixel_size_um is not None and source is not None
                and source.value == PIXEL_SIZE_SOURCE_DEFAULT):
            self._mm_pixel_size = True
            self.parameters[CAMERA_PIXEL_SIZE_PARAM].value = identity.pixel_size_um
            self._mark_pixel_size_source()
        self._finish_identity(identity)

    def finalize(self):
        """Stop the stream, unload an inline device, then the base class."""
        host = self._host
        if self._ready and host.is_connected:
            try:
                host.stop_stream(self._label)
            except Exception as exc:                      # noqa: BLE001 - finalizing must go on
                self.__logger.error('Stopping %s at finalize failed: %s', self._label, exc)
        self._unload_inline()
        super().finalize()
        self._setFinalizedStatus()
