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
- ``cameraPixelSizeUm`` -- sample-plane pixel size of the *unbinned* camera (wins over Micro-Manager's);
  ``pixelSizeUm`` / ``scale`` are that times the binning (the size of a recorded pixel)

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
import threading
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np

from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.status import DeviceConnectionState
from imswitch.imcontrol.model.managers.detectors.DetectorManager import (
    CAMERA_PIXEL_SIZE_PARAM,
    CAMERA_PIXEL_SIZE_SOURCE_PARAM,
    ChunkKind,
    PIXEL_SIZE_SOURCE_DEFAULT,
    PIXEL_SIZE_SOURCE_SETUP,
    QUEUED_FRAME_OVERHEAD_BYTES,
    RawFrameUnavailableError,
    configuredCameraPixelSize,
)
from imswitch.pluginapi import DetectorManager, DetectorNumberParameter

from . import _properties
from ._bridge import MicroManagerDeviceMixin
from ._core import FramesLostError, MMCoreHostError, StreamFault, StreamFaultKind, queue_budget_bytes
from ._frames import UnsupportedPixelTypeError, dtype_for_pixel_type_property

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
PROPERTY_PIXEL_TYPE = 'PixelType'

#: Parameter that carries the exposure, and where it sits in the settings tree.
PARAM_EXPOSURE = 'Exposure'
EXPOSURE_GROUP = 'Timings'
#: Micro-Manager's exposure unit (``setExposure`` / ``getExposure``).
EXPOSURE_UNITS = 'ms'
#: Value of the read-only ``Exposure`` placeholder a camera shows while its hub is down: "no exposure
#: yet". It is not a setting (the row is read-only); the real value replaces it on adoption.
ABSENT_EXPOSURE_MS = 0.0

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
    pixel_size_um: Optional[float]            # unbinned (MM's value divided by the binning); None = unknown


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
        # Live calibration (plan 4.4): MMCore's pixel size follows the objective. Its callbacks only
        # set ``_pixel_dirty`` (store-only, under ``_pixel_lock``); the value is re-read on the next
        # use of ``pixelSizeUm`` / ``parameters`` / ``refreshParameters``.
        self._pixel_lock = threading.Lock()
        self._pixel_dirty = False
        self._pixel_devices: frozenset = frozenset()   # devices behind Micro-Manager's pixel-size configs
        self._pixel_events_core = None            # the core the handlers are connected to
        self._pixel_conflict_logged: Optional[float] = None   # MM value last reported as overruled
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
            else:
                # The Settings panel builds its tree from the parameters present at start-up and has
                # no way to add a group later: declare the ones the camera will have once adopted.
                parameters[PARAM_EXPOSURE] = self._exposure_parameter(ABSENT_EXPOSURE_MS, editable=False)
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
            self._connect_pixel_events()
            self._setConnected(f'{self.model}: {self._label} connected')

    # ------------------------------------------------------------------ setup
    def _adopt_label(self) -> _Identity:
        """Resolve the label, register it with the host and read everything from Micro-Manager."""
        with self._host.lock:
            self._resolve_label()
            self._host.register_camera(self._label, self._is_recording)
            return self._read_identity()

    def _is_recording(self) -> bool:
        """True while a recording consumes this camera's frames (the host's "is recording" test).

        Leased *and* a ``RAW`` chunk consumer registered: the recording asks for ``RAW``
        (``DISPLAY`` is the viewer's). Leased alone is not enough: a recording leases all
        its cameras in one ``acquire`` before it registers its consumers, and live view
        leases too, so ``acquisitionLeased`` alone would refuse starting two equally
        sized cameras together and a second camera's live view.
        """
        if not self.acquisitionLeased:
            return False
        kinds = self._chunkKinds() or {}
        return any(kind is ChunkKind.RAW for kind in list(kinds.values()))

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
    def _exposure_parameter(value: float, *, editable: bool = True) -> DetectorNumberParameter:
        return DetectorNumberParameter(
            group=EXPOSURE_GROUP, value=float(value), valueUnits=EXPOSURE_UNITS, editable=editable)

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

    # ------------------------------------------------------- live pixel size
    def _pixel_size_devices(self, core) -> frozenset:
        """Labels of the devices Micro-Manager's pixel-size configs depend on (``Objective`` ...)."""
        devices = set()
        try:
            for config in core.getAvailablePixelSizeConfigs():
                data = core.getPixelSizeConfigData(config)
                devices.update(data.getSetting(i).getDeviceLabel() for i in range(data.size()))
        except Exception as exc:                          # noqa: BLE001 - fall back to following every device
            self.__logger.warning('Could not read the pixel-size configs of %s (%s); any property change '
                                  'will re-read the pixel size', self._label, exc)
            return frozenset()
        return frozenset(devices)

    def _connect_pixel_events(self) -> None:
        """Follow MMCore's pixel size: an objective change must reach the calibration (plan 4.4)."""
        with self._host.lock:
            self._disconnect_pixel_events()
            core = self._host.core
            self._pixel_devices = self._pixel_size_devices(core)
            core.events.pixelSizeChanged.connect(self._on_pixel_size_event)
            core.events.configSet.connect(self._on_pixel_config_event)
            core.events.propertyChanged.connect(self._on_pixel_property_event)
            self._pixel_events_core = core
            self._mark_pixel_size_dirty()

    def _disconnect_pixel_events(self) -> None:
        core, self._pixel_events_core = self._pixel_events_core, None
        if core is None:
            return
        for signal_name, handler in (('pixelSizeChanged', self._on_pixel_size_event),
                                     ('configSet', self._on_pixel_config_event),
                                     ('propertyChanged', self._on_pixel_property_event)):
            try:
                getattr(core.events, signal_name).disconnect(handler)
            except Exception:                          # noqa: BLE001 - not connected, or the core is already gone
                pass

    def _mark_pixel_size_dirty(self) -> None:
        with self._pixel_lock:
            self._pixel_dirty = True

    def _on_pixel_size_event(self, size_um) -> None:
        """MMCore's callback thread: store only (never the hub lock, never Qt)."""
        self._mark_pixel_size_dirty()

    def _on_pixel_config_event(self, group, config) -> None:
        """MMCore's callback thread: store only."""
        self._mark_pixel_size_dirty()

    def _on_pixel_property_event(self, device, prop, value) -> None:
        """MMCore's callback thread: store only. Only a device behind a pixel-size config matters."""
        if not self._pixel_devices or device in self._pixel_devices:
            self._mark_pixel_size_dirty()

    def _apply_pending_pixel_size(self) -> None:
        """Re-read the pixel size if a callback said it may have changed (see the callbacks above)."""
        if not self.__dict__.get('_ready'):
            return
        with self._pixel_lock:
            dirty, self._pixel_dirty = self._pixel_dirty, False
        if dirty:
            self._reread_pixel_size()

    def _reread_pixel_size(self) -> None:
        """Take Micro-Manager's current (unbinned) pixel size into the parameter, where it is allowed to.

        The source parameter decides: Micro-Manager's own value follows the objective; a placeholder
        becomes Micro-Manager's as soon as it has a value; a setup-file or user value is never
        overwritten (it is logged once per changed Micro-Manager value that the setup/user wins).
        """
        host = self._host
        if not host.is_connected:
            return
        with host.lock:
            size = self._micromanager_pixel_size(host.core)
            parameters = super().parameters
            param = parameters.get(CAMERA_PIXEL_SIZE_PARAM)
            source = parameters.get(CAMERA_PIXEL_SIZE_SOURCE_PARAM)
            if param is None or source is None:
                return
            if source.value == PIXEL_SIZE_SOURCE_MICROMANAGER:
                if size is None:
                    self.__logger.warning(
                        'Micro-Manager has no pixel size for its current state (no pixel-size config matches, '
                        'e.g. the objective has no calibration); keeping %.4g um', float(param.value))
                elif size != param.value:
                    self.__logger.info('Camera pixel size %.4g um -> %.4g um: Micro-Manager\'s pixel-size '
                                       'config changed', float(param.value), size)
                    param.value = size
            elif source.value == PIXEL_SIZE_SOURCE_DEFAULT and size is not None:
                self._mm_pixel_size = True               # the placeholder gives way to a real calibration
                param.value = size
                self._mark_pixel_size_source()
                self.__logger.info('Camera pixel size %.4g um: Micro-Manager\'s pixel-size config now has one', size)
            elif size is not None and size != param.value and size != self._pixel_conflict_logged:
                self._pixel_conflict_logged = size
                owner = 'the setup file' if source.value == PIXEL_SIZE_SOURCE_SETUP else 'you'
                self.__logger.warning(
                    "Micro-Manager's pixel size changed to %.4g um (the objective or its pixel-size config), but "
                    "%s set %.4g um for camera %s and that value stays in effect (source: %s)",
                    size, owner, float(param.value), self._label, source.value)

    # ------------------------------------------------------------- properties
    @property
    def configOwnedParameters(self) -> frozenset:
        """Also the pixel size when Micro-Manager supplied it: a saved widget state must not
        reinstate an old value over the configuration that is authoritative at every start."""
        owned = super().configOwnedParameters
        return owned | {CAMERA_PIXEL_SIZE_PARAM} if self._mm_pixel_size else owned

    @property
    def parameters(self) -> Dict[str, Any]:
        """The parameters; a pixel size that Micro-Manager changed since the last read is applied first."""
        self._apply_pending_pixel_size()
        return super().parameters

    @property
    def pixelSizeUm(self) -> List[float]:
        """``[1, p * b, p * b]``: the optically effective size of a *recorded* pixel.

        ImSwitch2 gives this size to recording metadata (OME ``PhysicalSize``), the napari layer
        scale and tiling unchanged, so at binning ``b`` it is ``b`` times the unbinned size ``p`` of
        the ``Camera pixel size`` parameter, which keeps meaning the unbinned sample-plane value
        (what the setup file, the user and Micro-Manager's pixel-size config state). ``scale``
        follows from the base class.
        """
        self._apply_pending_pixel_size()
        z, y, x = super().pixelSizeUm
        binning = max(1, int(self.binning))
        return [z, y * binning, x * binning]

    @property
    def dtype(self) -> np.dtype:
        """The dtype the camera delivers now (``PixelType``), known before the first frame."""
        cached = self.__dict__.get('_stream_dtype')
        return cached if cached is not None else super().dtype

    def _stream_format(self) -> Tuple[Tuple[int, int], np.dtype]:
        return self._stream_hw, self._stream_dtype

    # ------------------------------------------------------------- parameters
    def setParameter(self, name, value):
        """Write Exposure or a Micro-Manager property and read it back; any property other than
        Exposure restarts a running stream (MMCore cannot say which ones change the frame)."""
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
        """Write one Micro-Manager property (stopped around the write if streaming); the value read back.

        While another camera streams, the frame format of this one must stay the other's
        (MMCore's one buffer, Micro-Manager PR #936), and so must the format of MMCore's
        *current* camera whichever camera that is. So a property that can change the format is
        checked BEFORE it is written when its effect is known (``PixelType``: the dtype follows
        from the value), and otherwise written, re-read and **rolled back** when the new
        format clashes -- the stream is restarted with the old one either way and the refusal is
        raised to the caller.
        """
        host, label = self._host, self._label
        text = _properties.coerce_for_mm(self.parameters[name], value)
        written: Dict[str, Any] = {}                 # what to put back: set once the write happened
        # The dtype the write selects, when it is known from the value: the only way to learn it for a
        # camera that is not MMCore's current one while another camera streams (see _sync_geometry).
        predicted = dtype_for_pixel_type_property(text) if name == PROPERTY_PIXEL_TYPE else None

        def action():
            with host.lock:
                core = host.core
                previous = core.getProperty(label, name)
                core.setProperty(label, name, text)          # MMCore's refusal propagates unchanged
                written['previous'] = previous
                written['dtype'] = self._stream_dtype
                try:
                    self._sync_geometry(dtype_hint=predicted)
                except UnsupportedPixelTypeError as exc:
                    core.setProperty(label, name, previous)  # the bridge cannot carry this: undo it
                    del written['previous']
                    raise UnsupportedPixelTypeError(_COLOUR_REFUSAL.format(label=label, reason=exc)) from exc
                return _properties.read_back(core, label, name)

        def rollback():
            """Put the old text back and re-read the format (after a format clash)."""
            with host.lock:
                previous = written.pop('previous', None)
                if previous is not None:
                    host.core.setProperty(label, name, previous)
                    self._sync_geometry(dtype_hint=written.get('dtype'))

        def precheck():
            if predicted is not None and self._stream_dtype is not None:
                host.check_format_change_allowed(label, self._stream_hw, predicted)

        def after(applied):
            self._edited[name] = str(applied)
            self.refreshParameters()
            self._warn_if_changed(name, value, applied)
            return applied

        return self._safe_camera_action(action, after=after, precheck=precheck, rollback=rollback)

    def _warn_if_changed(self, name, requested, applied) -> None:
        try:
            same = float(requested) == float(applied)
        except (TypeError, ValueError):
            same = str(requested) == str(applied)
        if not same:
            self.__logger.warning('%s: asked for %r, the camera took %r', name, requested, applied)

    def refreshParameters(self) -> None:
        """Re-read every mapped Micro-Manager property, the exposure and Micro-Manager's pixel size."""
        self._requireConnected('refreshParameters')
        with self._host.lock:
            core = self._host.core
            _properties.refresh_parameters(core, self._label, self.parameters, self._mm_names)
            self.parameters[PARAM_EXPOSURE].value = float(core.getExposure(self._label))
            self._reread_pixel_size()                     # the objective may have changed behind our back

    def setBinning(self, binning):
        """Set the ``Binning`` property to a supported value and re-read the frame geometry."""
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

        def after(_result):
            super(MicroManagerCameraManager, self).setBinning(binning)
            self.refreshParameters()

        self._safe_camera_action(action, after=after)

    @staticmethod
    def _roi_fits(roi, width, height) -> bool:
        x, y, w, h = roi
        return w > 0 and h > 0 and x >= 0 and y >= 0 and x + w <= width and y + h <= height

    # --------------------------------------------------- geometry and format
    def _sync_geometry(self, dtype_hint=None) -> bool:
        """Re-read ROI and frame format from MMCore into the manager; True if anything changed.

        ``dtype_hint`` is the dtype the caller knows the camera now delivers (a ``PixelType`` write),
        used only when the format cannot be read because another camera streams.

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
                # cannot be read. The frame size follows the ROI; the dtype is the caller's hint
                # or stays as known, and the host checks every frame against it.
                stream_hw, dtype = (h, w), (self._stream_dtype if dtype_hint is None else dtype_hint)
            self._frameStart, self._shape = (x, y), (w, h)
            self._stream_hw, self._stream_dtype = stream_hw, np.dtype(dtype)
            changed = before != (self._frameStart, self._shape, self._stream_hw, self._stream_dtype)
            if changed:
                self._latest = None                       # the old frame no longer matches the new geometry
                if not host.running_streams():
                    full_width, full_height, _ = host.sensor_geometry(label)
                    self._setFullShape((full_width, full_height))
            return changed

    def _safe_camera_action(self, action, after=None, *, precheck=None, rollback=None):
        """stop the stream -> ``action()`` -> restart it, for settings MMCore may refuse mid-sequence.

        The format is re-read by ``action`` (via :meth:`_sync_geometry`) before the restart,
        so a changed frame size or pixel type is what the new sequence is started with. The
        action's own error is the one raised.

        ``precheck()`` runs first, before anything is stopped or written, and raises to refuse
        a change whose effect is known to clash with another running stream. ``rollback()``
        undoes the change: it runs when the format the action produced clashes with another
        running stream (or MMCore's current camera), or the restart is refused, so the stream
        is never left stopped and MMCore's current camera never keeps a format other than the
        running streams'. The stream then restarts with the old format and the refusal is raised.

        A stream fault found while stopping (:class:`FramesLostError`: the sequence had
        already ended, or ended unnoticed, before this call) must not cancel the change or
        the restart, and must not be swallowed by the caller of a settings change -- the
        recording that lost the frames has to fail, not hang on a stream nobody restarted.
        So the stop's fault is captured, the action runs, the stream restarts if it was
        running, the fault is put back on the stream (:meth:`MMCoreHost.restore_fault`;
        sticky like any other host fault, so the recording's next ``getChunk`` raises it
        and ``stopAcquisition`` raises it too, which is what makes the lease table set the
        hardware fault), and ``after(result)`` (the caller's bookkeeping, run only when the
        action succeeded) still runs. Last, the fault is raised to the caller of the change
        as well, because the recording it belongs to is lost. A fault that was already
        recorded before the call (stream no longer streaming) is left pending as it is.
        """
        host, label = self._host, self._label
        with host.lock:
            stream = host.register_camera(label)
            was_streaming = stream.streaming
            fault: Optional[StreamFault] = None
            if was_streaming:
                host.check_buffer_may_be_cleared(label)   # refuse before stopping what cannot be restarted
            if precheck is not None:
                precheck()                                # refuse before anything is written
            if was_streaming:
                try:
                    host.stop_stream(label)
                except FramesLostError as exc:            # the sequence is stopped; carry on, keep the fault
                    fault = exc.fault
                    self.__logger.error('Camera %s lost frames before a settings change: %s '
                                        '(stopAcquisition raises it)', label, exc)
            error: Optional[BaseException] = None
            result = None
            try:
                result = action()
                if self._stream_dtype is not None:
                    host.check_format_change_allowed(label, self._stream_hw, self._stream_dtype)
            except Exception as exc:                      # noqa: BLE001 - re-raised below, after the restart
                error = exc
                if rollback is not None and isinstance(exc, MMCoreHostError):
                    self._undo(rollback, 'the format clashes with another running camera')
            if was_streaming:
                try:
                    host.start_stream(label, *self._stream_format())
                except Exception as exc:                  # noqa: BLE001
                    if error is None:
                        error = exc
                        if rollback is not None:
                            # The change went through but the stream cannot run with it: undo it and
                            # bring the old stream back instead of leaving the camera stopped.
                            self._undo(rollback, f'restarting with the new format was refused ({exc})')
                            try:
                                host.start_stream(label, *self._stream_format())
                            except Exception as again:    # noqa: BLE001
                                self.__logger.error('Restarting %s with its old format also failed: %s', label, again)
                    else:
                        self.__logger.error('Restarting %s after a failed setting also failed: %s', label, exc)
            if fault is not None:
                host.restore_fault(label, fault)
            if error is not None:
                raise error
            if after is not None:
                result = after(result)
            if fault is not None:
                raise FramesLostError(label, fault)
            return result

    def _undo(self, rollback, reason: str) -> None:
        """Run a settings rollback; a failing rollback is logged, it must not hide the original error."""
        try:
            rollback()
        except Exception as exc:                          # noqa: BLE001 - the original error is raised
            self.__logger.error('Camera %s: undoing the setting (%s) failed: %s', self._label, reason, exc)
        else:
            self.__logger.warning('Camera %s: the setting was undone: %s', self._label, reason)

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
        """Start this camera's sequence through the host, which guards frame shape and buffer ownership."""
        self._requireConnected('startAcquisition')
        host = self._host
        with host.lock:
            host.register_camera(self._label)
            if not host.running_streams():
                self._sync_geometry()                     # pick up changes made behind the manager's back
            host.start_stream(self._label, *self._stream_format())
            self._clear_lost_frames_status()

    def stopAcquisition(self):
        """Stop the stream; MMCore errors and :class:`FramesLostError` propagate (the lease
        table turns them into the hardware fault). With the hub down nothing runs any more."""
        if not (self._ready and self._host.is_connected):
            return
        try:
            self._host.stop_stream(self._label)           # raises (and consumes) a pending stream fault
        finally:
            self._clear_lost_frames_status()
        self._raise_staged_fault()

    def flushBuffers(self):
        """Drop this camera's queued and staged frames; other cameras are not touched."""
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
                    # Live view keeps the last frame, which looks alive: say in the status that
                    # frames were lost. Still CONNECTED (the device is fine, and a command must
                    # not be refused for it); the supervisor and the UI read it from statusSnapshot().
                    self._setConnectionState(
                        DeviceConnectionState.CONNECTED,
                        summary=f'{self.model}: {self._label} lost frames, the live image is stale',
                        details=exc.fault.message)
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

    def _clear_lost_frames_status(self) -> None:
        """The stream was restarted or its fault consumed: drop the "lost frames" status the live view set."""
        if self._lost_reported is None:
            return
        self._lost_reported = None
        if self.connectionState is DeviceConnectionState.CONNECTED:
            self._setConnected(f'{self.model}: {self._label} connected')

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
            host.register_camera(self._label, self._is_recording)
            core = host.core
            label = self._label
            expected_hw, expected_dtype = self._stream_hw, self._stream_dtype
            expected_roi = (*self._frameStart, *self._shape)
            expected_full = self.fullShape
            self._host.acknowledge_fault(label)
            self._lost_reported = None                    # the reconnect sets the status itself
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
                if host.running_streams():
                    # Another camera resumed first (the lifecycle service reconnects the cameras one
                    # after the other). ``sensor_geometry`` is idle-only (it clears the ROI, which
                    # clears the shared buffer), so the full frame is the one cached from the
                    # adoption -- it does not change across a reconnect of the same device -- and
                    # the reloaded device is checked by reads only (``getROI``).
                    full_width, full_height = (int(v) for v in expected_full)
                else:
                    full_width, full_height, _ = host.sensor_geometry(label)
                    self._setFullShape((full_width, full_height))
                    if (full_width, full_height) != tuple(expected_full):
                        errors.append(f'the sensor is now {full_width}x{full_height}, was {expected_full[0]}x{expected_full[1]}')
                if expected_roi != (0, 0, full_width, full_height):
                    applied = host.set_roi(label, *expected_roi)
                    if applied != expected_roi:
                        errors.append(f'ROI {expected_roi} came back as {applied}')
                found_roi = tuple(int(v) for v in host.call('getROI', label))
                if found_roi != expected_roi:
                    errors.append(f'the ROI is now {found_roi}, was {expected_roi} (x, y, width, height)')
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
                self._connect_pixel_events()              # the reloaded core is a new object
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
        exposure = existing.pop(PARAM_EXPOSURE, None)
        if exposure is None:
            exposure = self._exposure_parameter(identity.exposure)
        else:
            # The placeholder row exists in the Settings tree already: the same parameter becomes the
            # real one, in place, so the row's group and name stay what the tree was built with.
            exposure.value, exposure.editable = float(identity.exposure), True
        new = {PARAM_EXPOSURE: exposure, **identity.properties}
        self.parameters.clear()
        self.parameters.update({**new, **existing})
        source = self.parameters.get(CAMERA_PIXEL_SIZE_SOURCE_PARAM)
        if (identity.pixel_size_um is not None and source is not None
                and source.value == PIXEL_SIZE_SOURCE_DEFAULT):
            self._mm_pixel_size = True
            self.parameters[CAMERA_PIXEL_SIZE_PARAM].value = identity.pixel_size_um
            self._mark_pixel_size_source()
        self._finish_identity(identity)
        self._connect_pixel_events()

    def finalize(self):
        """Stop the stream, unload an inline device, then the base class."""
        self._disconnect_pixel_events()
        host = self._host
        if self._ready and host.is_connected:
            try:
                host.stop_stream(self._label)
            except Exception as exc:                      # noqa: BLE001 - finalizing must go on
                self.__logger.error('Stopping %s at finalize failed: %s', self._label, exc)
        self._unload_inline()
        super().finalize()
        self._setFinalizedStatus()
