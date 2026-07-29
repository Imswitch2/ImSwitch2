"""The Imaging Source camera driver built on IC Imaging Control 4 (IC4).

Replaces the legacy ``pyicic`` ctypes wrapper around the IC3 ``tisgrabber`` DLL.
See ``docs/design/plans/tis-camera-ic4-migration.md`` in the ImSwitch repository
for why, and for the rig-validation gate this code is still waiting on.

The API shape here follows The Imaging Source's own published example
(``ic4-examples/python/image-acquisition/save-bmp-on-trigger``) rather than the
prose documentation, because four details that the prose glosses over are all
load-bearing:

* ``ic4.Library.init()`` is mandatory — nothing works before it.
* ``QueueSinkListener.sink_connected`` is required and must return ``True``,
  or the stream never connects.
* ``buffer.numpy_copy()``, never ``numpy_wrap()``: the wrap is a view onto a
  buffer the SDK recycles, and must not outlive the callback.
* ``stream_stop()`` must run before the listener becomes collectable.

``MockIC4Camera`` mirrors the real class and needs no SDK, so the manager and its
tests run headless.
"""

from __future__ import annotations

import logging
import re

import numpy as np

from ._frame_queue import FrameQueue

try:
    from imswitch.imcommon.model import initLogger
except Exception:  # pragma: no cover - only when run outside ImSwitch
    def initLogger(owner, instanceName=None):
        return logging.getLogger(str(owner))

#: Routed through ImSwitch's ``initLogger`` rather than
#: ``logging.getLogger(__name__)``. ImSwitch installs its handler on the
#: ``imswitch`` logger alone (``imcommon/model/logging.py``), and
#: ``imswitch_device_tis._ic4_driver`` is a separate top-level tree with no
#: handler on it — so every record this module emitted below WARNING was
#: discarded, and its warnings fell through to ``logging.lastResort`` (bare
#: stderr, no timestamp, outside the ImSwitch log). That silently included
#: ``_log_state``, the one line that reports what the camera is actually doing.
logger = initLogger('IC4Camera')

#: Default microseconds of exposure, matching the DMK 33UX250's usable range.
DEFAULT_EXPOSURE_US = 5000.0


def ensure_library_initialized(ic4) -> None:
    """Initialize the IC4 library exactly once per process.

    ``Library.init()`` is *not* idempotent — it raises
    ``RuntimeError("Library.init was already called")`` when the library is
    already up, and exposes no public predicate to test for that. A setup with
    two TIS cameras constructs this driver twice in one process, so calling it
    unguarded makes the second camera fail to open.

    That failure is especially nasty here because the manager's constructor
    falls back to the mock on RuntimeError: the second camera would come up
    silently showing synthetic frames. Swallow only the already-initialized
    case; a genuine failure to load the native library raises FileNotFoundError
    and must still propagate.
    """
    try:
        ic4.Library.init()
    except RuntimeError:
        logger.debug("IC4 library was already initialized by another component")


def normalize_frame(frame: np.ndarray) -> np.ndarray:
    """Drop the trailing singleton channel axis IC4 returns for mono formats.

    ``ImageBuffer.numpy_copy()`` yields **(H, W, 1)** for Mono8/Mono16 — measured
    on a DMK 33UX250, not assumed. That extra axis has to come off here:

    - ``DetectorManager.getLatestFrame`` is contracted to return (H, W);
    - ``readChunk`` does ``list.extend`` over a chunk, which iterates axis 0, so
      a (N, H, W, 1) chunk would hand the recorder 3-D "frames" and corrupt the
      saved file exactly the way a 2-D chunk did on ThorCam TSI.

    Real and mock frames both pass through this, so the mock cannot drift back to
    a shape the hardware never produces — which is what let this reach a commit.
    """
    if frame.ndim == 3 and frame.shape[2] == 1:
        return frame[:, :, 0]
    return frame


def dtype_for_pixel_format(pixel_format) -> np.dtype:
    """Map a GenICam PixelFormat name to the numpy dtype frames arrive in.

    Anything above 8 bits — ``Mono10``, ``Mono12p``, ``Mono16`` — is delivered in
    16-bit containers. Unknown or absent formats fall back to uint16, the safe
    direction: over-allocating a container never truncates sample values.
    """
    if not pixel_format:
        return np.dtype(np.uint16)
    match = re.search(r"(\d+)", str(pixel_format))
    bits = int(match.group(1)) if match else 16
    return np.dtype(np.uint8) if bits <= 8 else np.dtype(np.uint16)


class IC4Camera:
    """IC4-backed TIS camera with push-based frame delivery.

    Args:
        serial: Camera serial number. ``None`` opens the first device found.
        max_queued_frames: Bound on the retained-frame queue.
        pixel_format: Optional GenICam ``PixelFormat`` value, e.g. ``"Mono16"``.
            ``None`` leaves the device default.
        reset_to_default: Load the device's ``Default`` user set on open. Strongly
            recommended: this camera has historically been configured by hand in
            the vendor GUI, so it will otherwise carry arbitrary leftover state.
    """

    def __init__(
        self,
        serial=None,
        max_queued_frames: int = 256,
        pixel_format=None,
        reset_to_default: bool = True,
    ):
        try:
            import imagingcontrol4 as ic4
        except ImportError as e:  # pragma: no cover - requires the vendor SDK
            raise ImportError(
                "imagingcontrol4 not found. Install with: "
                "pip install imswitch-device-tis[hardware]. Note this also "
                "requires the IC4 GenTL Producer (USB3 Vision) to be installed "
                "on the machine."
            ) from e

        self._ic4 = ic4
        ensure_library_initialized(ic4)

        devices = ic4.DeviceEnum.devices()
        if not devices:
            raise RuntimeError(
                "No IC4 devices found. Check the USB connection and that the "
                "IC4 GenTL Producer (USB3 Vision) is installed — a camera still "
                "bound only to the legacy IC3 driver will not enumerate here."
            )

        if serial is None:
            dev_info = devices[0]
        else:
            matches = [d for d in devices if d.serial == str(serial)]
            if not matches:
                available = ", ".join(f"{d.model_name} ({d.serial})" for d in devices)
                raise ValueError(
                    f"IC4 camera with serial '{serial}' not found. Available: {available}"
                )
            dev_info = matches[0]

        self._grabber = ic4.Grabber(dev_info)
        self._pm = self._grabber.device_property_map
        self._serial = dev_info.serial
        self._model = dev_info.model_name

        if reset_to_default:
            # try_set_value: not every model supports user sets. On a command
            # node (which UserSetLoad is) IC4 executes the command when the
            # value is 1 — see PropertyMap.try_set_value's documented dispatch.
            self._pm.try_set_value(ic4.PropId.USER_SET_SELECTOR, "Default")
            self._pm.try_set_value(ic4.PropId.USER_SET_LOAD, 1)

        # Order matters: both of these undo state the user set above just
        # restored, so they cannot run before it.
        self._ensure_manual_exposure_and_gain()
        self._disarm_all_triggers()

        if pixel_format is not None:
            self._pm.set_value(ic4.PropId.PIXEL_FORMAT, pixel_format)

        # Read back rather than trusting the request: the device may not have
        # accepted it, and when pixel_format is None we do not know the default.
        try:
            activeFormat = self._pm.get_value_str(ic4.PropId.PIXEL_FORMAT)
        except Exception as e:
            logger.debug(f"Could not read PixelFormat, assuming 16-bit: {e}")
            activeFormat = None
        self._dtype = dtype_for_pixel_format(activeFormat)

        self._queue = FrameQueue(maxlen=max_queued_frames, logger=logger)
        self._listener = _QueueListener(self._queue)
        self._sink = None
        self._streaming = False

        logger.info(f"Opened IC4 camera: {self._model} ({self._serial})")
        self._log_state("after open")

    # -- identity ---------------------------------------------------------

    @property
    def serial(self):
        return self._serial

    @property
    def model(self):
        return self._model

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    @property
    def dtype(self) -> np.dtype:
        """dtype frames arrive in, known from PixelFormat at open time.

        The manager surfaces this as its authoritative dtype rather than
        inheriting ``DetectorManager.dtype``, which infers from the last live-view
        frame and reports uint16 until one exists — wrong for a Mono8 camera, and
        wrong at exactly the moment a recording is being set up.
        """
        return self._dtype

    # -- properties -------------------------------------------------------

    @property
    def sensor_width_pixels(self) -> int:
        return int(self._pm.get_value_int(self._ic4.PropId.WIDTH_MAX))

    @property
    def sensor_height_pixels(self) -> int:
        return int(self._pm.get_value_int(self._ic4.PropId.HEIGHT_MAX))

    @property
    def image_width_pixels(self) -> int:
        return int(self._pm.get_value_int(self._ic4.PropId.WIDTH))

    @property
    def image_height_pixels(self) -> int:
        return int(self._pm.get_value_int(self._ic4.PropId.HEIGHT))

    def _ensure_manual_exposure_and_gain(self) -> None:
        """Switch ``ExposureAuto`` and ``GainAuto`` off.

        Without this the camera silently ignores every exposure and gain value
        ImSwitch writes: the device's auto algorithm owns ``ExposureTime`` and
        ``Gain`` while it is running, and re-derives them from scene brightness
        within a frame or two. On the DMK 33UX250 that lands at 1/30 s and rails
        the gain at 48 dB, which is exactly the "it resets itself" symptom.

        This is not a leftover-configuration problem, so it is not enough to fix
        it once by hand in the vendor GUI: the ``Default`` user set loaded in
        ``__init__`` has both autos set to ``Continuous``, so *opening the
        camera is what turns them back on*. Hence this runs right after the user
        set is loaded, and again before every exposure/gain write — the vendor
        GUI can be opened alongside ImSwitch and re-enable them mid-session.
        """
        pm, pid = self._pm, self._ic4.PropId
        for prop_id, label in ((pid.EXPOSURE_AUTO, "ExposureAuto"),
                               (pid.GAIN_AUTO, "GainAuto")):
            # try_set_value: a model without the property is fine — it has no
            # auto algorithm to fight with.
            if not pm.try_set_value(prop_id, "Off"):
                logger.warning(
                    f"Could not set {label}=Off. If exposure or gain will not "
                    f"hold a value, this is why."
                )

    def _warn_if_not_applied(self, label, requested, actual) -> None:
        """Warn when the device did not take a value we asked for.

        Silent clamping is normal and fine (asking for 1 µs on a sensor with a
        20 µs floor), so the tolerance is generous; the point is to catch the
        case where a value is ignored outright and the log still reads as if it
        had been applied.
        """
        if requested and abs(actual - requested) / abs(requested) > 0.05:
            logger.warning(
                f"{label}: requested {requested:g}, device reports {actual:g}. "
                f"The value was clamped to the device's legal range, or an auto "
                f"feature is overriding it."
            )

    def set_exposure_us(self, exposure_us) -> float:
        """Set the exposure and return what the device actually took."""
        self._ensure_manual_exposure_and_gain()
        self._pm.set_value(self._ic4.PropId.EXPOSURE_TIME, float(exposure_us))
        applied = self.get_exposure_us()
        self._warn_if_not_applied("ExposureTime", float(exposure_us), applied)
        return applied

    def get_exposure_us(self) -> float:
        return float(self._pm.get_value_float(self._ic4.PropId.EXPOSURE_TIME))

    def set_gain(self, gain) -> float:
        """Set the gain and return what the device actually took."""
        self._ensure_manual_exposure_and_gain()
        self._pm.set_value(self._ic4.PropId.GAIN, float(gain))
        applied = self.get_gain()
        self._warn_if_not_applied("Gain", float(gain), applied)
        return applied

    def get_gain(self) -> float:
        return float(self._pm.get_value_float(self._ic4.PropId.GAIN))

    def _fit_to_property(self, prop_id, value) -> int:
        """Clamp ``value`` into a GenICam integer property's legal range/step.

        Sensors constrain Width/Height/OffsetX/OffsetY to a minimum, a maximum
        and an *increment* (commonly 4 or 8 px). ImSwitch's ROI selector hands
        over arbitrary pixel counts, and IC4 raises on a value that violates any
        of those — which would surface as an exception thrown out of the GUI's
        crop path. Rounding down to the increment keeps the ROI inside what the
        user selected.

        Falls back to the raw value if the property does not expose these
        attributes, so an unexpected SDK shape degrades to previous behaviour
        rather than breaking.
        """
        value = int(value)
        try:
            prop = self._pm.find_integer(prop_id)
            minimum, maximum = int(prop.minimum), int(prop.maximum)
            increment = int(prop.increment) or 1
        except Exception as e:
            logger.debug(f"Could not read constraints for {prop_id}: {e}")
            return value

        value = max(minimum, min(value, maximum))
        if increment > 1:
            value -= (value - minimum) % increment
        return value

    def set_roi(self, x0, y0, width, height) -> tuple:
        """Set the readout region and return the ``(x0, y0, width, height)`` the
        device actually took. Requires the stream to be stopped.

        The return value is not a formality: ``_fit_to_property`` rounds the
        request down to the sensor's increment, so the applied region routinely
        differs from what was asked for. A caller that assumes its request was
        honoured ends up describing frames the camera is not producing.
        """
        pm, pid = self._pm, self._ic4.PropId
        # Offsets to zero first, so a larger width/height is never rejected for
        # overflowing the sensor while an old offset is still applied. The
        # size constraints are read in that state for the same reason.
        pm.set_value(pid.OFFSET_X, 0)
        pm.set_value(pid.OFFSET_Y, 0)
        pm.set_value(pid.WIDTH, self._fit_to_property(pid.WIDTH, width))
        pm.set_value(pid.HEIGHT, self._fit_to_property(pid.HEIGHT, height))
        pm.set_value(pid.OFFSET_X, self._fit_to_property(pid.OFFSET_X, x0))
        pm.set_value(pid.OFFSET_Y, self._fit_to_property(pid.OFFSET_Y, y0))
        # Read back rather than returning the fitted values: the device is the
        # authority, and it may constrain a combination that each property
        # accepts on its own.
        return (
            int(pm.get_value_int(pid.OFFSET_X)),
            int(pm.get_value_int(pid.OFFSET_Y)),
            int(pm.get_value_int(pid.WIDTH)),
            int(pm.get_value_int(pid.HEIGHT)),
        )

    # -- trigger ----------------------------------------------------------

    def set_trigger_enabled(self, enabled: bool, source=None, activation=None) -> None:
        """Arm or disarm the hardware trigger in software.

        The legacy path could not do this — ``pyicic``'s ``enable_trigger`` was
        called once with ``False`` at init and swallowed its own error return, so
        arming was a manual step in the vendor's properties dialog.

        ``activation`` selects the edge (``RisingEdge`` / ``FallingEdge``). The
        DMK 33UX250 was found sitting on ``FallingEdge`` out of the box, which
        latches on the *trailing* edge of a TriggerScope pulse — frames still
        arrive, delayed by the pulse width, which is a subtle way to get skewed
        timing rather than an obvious failure.

        The transition is bracketed by a state snapshot because exposure and
        gain are reported to move across it, and nothing here writes them. Two
        mechanisms could do that and they need opposite fixes: ``ExposureTime``
        has a maximum coupled to ``AcquisitionFrameRate``, so *disarming* can
        re-impose the frame-period cap and clamp a long exposure; ``Gain`` has
        no such coupling, so a gain change points at ``GainAuto`` re-engaging
        instead. Only the device's own values, read either side of the write,
        distinguish those — and from the third case, where the hardware never
        moved and only ImSwitch's cached parameter is stale. This is diagnostic
        and deliberately changes no behaviour.
        """
        pm, pid = self._pm, self._ic4.PropId
        self._log_state(
            f"before TriggerMode -> {'On' if enabled else 'Off'}",
            level=logging.DEBUG,
        )
        # TriggerSelector must be set before TriggerMode on models that expose
        # it; try_set_value tolerates those that do not.
        pm.try_set_value(pid.TRIGGER_SELECTOR, "FrameStart")
        pm.set_value(pid.TRIGGER_MODE, "On" if enabled else "Off")
        if enabled and source is not None:
            pm.set_value(pid.TRIGGER_SOURCE, str(source))
        if enabled and activation is not None:
            pm.try_set_value(pid.TRIGGER_ACTIVATION, str(activation))

        # Read back: an armed trigger the software believes is disarmed looks
        # exactly like a dead camera — the stream connects, no frames ever
        # arrive, and nothing in the log says why.
        expected = "On" if enabled else "Off"
        actual = pm.get_value_str(pid.TRIGGER_MODE)
        if actual != expected:
            logger.warning(
                f"TriggerMode reads {actual!r} after setting it to {expected!r}."
            )

        self._log_state(f"after TriggerMode -> {expected}", level=logging.DEBUG)

    def _disarm_all_triggers(self) -> None:
        """Set ``TriggerMode=Off`` for *every* ``TriggerSelector`` entry.

        ``TriggerMode`` is per-selector in GenICam: the value read back depends
        on which ``TriggerSelector`` is active. Disarming only ``FrameStart``
        therefore leaves any other armed trigger — ``AcquisitionStart`` and
        ``ExposureStart`` are the ones models in this family expose — still
        holding the stream, while every reachable read says ``Off``.

        A trigger left armed by a previous session survives in device state
        (``dispose`` disarms, but a crash or a kill skips it), which is the
        shape of "live view is dead until I toggle the trigger once".

        Best-effort throughout: a model with no selector still gets the plain
        ``TriggerMode=Off`` write in the fallback.
        """
        pm, pid = self._pm, self._ic4.PropId
        try:
            selector = pm.find_enumeration(pid.TRIGGER_SELECTOR)
            entries = [entry.name for entry in selector.entries]
        except Exception as e:
            logger.debug(f"No TriggerSelector to enumerate ({e}); disarming directly")
            pm.try_set_value(pid.TRIGGER_MODE, "Off")
            return

        for name in entries:
            if not pm.try_set_value(pid.TRIGGER_SELECTOR, name):
                continue
            was_on = pm.try_set_value(pid.TRIGGER_MODE, "Off")
            if was_on:
                logger.debug(f"Disarmed TriggerSelector={name}")

        # Leave the selector on FrameStart: that is the one set_trigger_enabled
        # arms, and the one a scan expects to be current.
        pm.try_set_value(pid.TRIGGER_SELECTOR, "FrameStart")

    def _log_state(self, when: str, *, level: int = logging.INFO) -> None:
        """Log the properties that decide whether frames flow and how bright.

        These five have each already cost a debugging session on this rig, and
        every one of them can be changed behind ImSwitch's back by the vendor
        GUI or by a previous run. One line at open turns "the camera is acting
        up again" into a fact.

        ``level`` exists so the same snapshot can be taken often enough to
        bracket a state change without flooding the console at INFO.
        """
        pid = self._ic4.PropId
        fields = []
        for label, prop_id, getter in (
            ("TriggerMode", pid.TRIGGER_MODE, "get_value_str"),
            ("ExposureAuto", pid.EXPOSURE_AUTO, "get_value_str"),
            ("ExposureTime", pid.EXPOSURE_TIME, "get_value_float"),
            ("GainAuto", pid.GAIN_AUTO, "get_value_str"),
            ("Gain", pid.GAIN, "get_value_float"),
        ):
            try:
                fields.append(f"{label}={getattr(self._pm, getter)(prop_id)}")
            except Exception:
                fields.append(f"{label}=<unavailable>")
        logger.log(level, f"IC4 camera state {when}: {', '.join(fields)}")

    def is_trigger_enabled(self) -> bool:
        return self._pm.get_value_str(self._ic4.PropId.TRIGGER_MODE) == "On"

    def execute_software_trigger(self) -> None:
        """Fire one software trigger.

        This is what lets the whole capture path be validated before the
        TriggerScope is wired up: if software triggers yield distinct frames but
        TTL pulses do not, the fault is wiring or TriggerSource, not this code.
        """
        self._pm.execute_command(self._ic4.PropId.TRIGGER_SOFTWARE)

    # -- streaming --------------------------------------------------------

    def start_stream(self) -> None:
        if self._streaming:
            return
        self._sink = self._ic4.QueueSink(self._listener)
        self._grabber.stream_setup(
            self._sink,
            setup_option=self._ic4.StreamSetupOption.ACQUISITION_START,
        )
        self._streaming = True
        logger.debug("IC4 stream started")

    def stop_stream(self) -> None:
        if not self._streaming:
            return
        self._grabber.stream_stop()
        self._streaming = False
        logger.debug("IC4 stream stopped")

    # -- frames -----------------------------------------------------------

    def pop_frames(self) -> list:
        """Drain and return every frame delivered since the last call."""
        return self._queue.drain()

    def latest_frame(self):
        """Newest retained frame without draining, or None."""
        return self._queue.latest()

    def flush(self) -> None:
        self._queue.clear()

    @property
    def queued_frame_count(self) -> int:
        return len(self._queue)

    @property
    def dropped_frame_count(self) -> int:
        return self._queue.dropped

    # -- teardown ---------------------------------------------------------

    def dispose(self) -> None:
        """Stop the stream, disarm, and close the device.

        Order matters: ``stream_stop()`` must precede the listener becoming
        collectable, per the vendor's own example. Idempotent.
        """
        if self._grabber is None:
            return
        try:
            self.stop_stream()
        except Exception as e:
            logger.warning(f"IC4 stream_stop failed during dispose: {e}")
        try:
            self._pm.set_value(self._ic4.PropId.TRIGGER_MODE, "Off")
        except Exception as e:
            logger.debug(f"IC4 trigger disarm ignored during dispose: {e}")
        try:
            self._grabber.device_close()
        except Exception as e:
            logger.warning(f"IC4 device_close failed: {e}")
        finally:
            self._grabber = None
            self._sink = None
            self._listener = None
            logger.info(f"Disposed IC4 camera {self._serial}")


def _make_listener_base():
    """Build the QueueSinkListener base, or ``object`` when the SDK is absent.

    Keeps this module importable (and the mock path testable) on machines with
    no ``imagingcontrol4`` installed.
    """
    try:
        import imagingcontrol4 as ic4

        return ic4.QueueSinkListener
    except ImportError:
        return object


class _QueueListener(_make_listener_base()):
    """Pushes every delivered frame into a :class:`FrameQueue`."""

    def __init__(self, queue: FrameQueue):
        super().__init__()
        self._queue = queue

    #: Buffers to allocate beyond the SDK's stated minimum. The sink allocates
    #: the minimum itself if we don't, but headroom absorbs a consumer that
    #: stalls briefly — a 5 MP sensor at 75 fps fills the minimum quickly.
    EXTRA_BUFFERS = 8

    def sink_connected(self, sink, image_type, min_buffers_required) -> bool:
        # Required by the SDK and must return True, or the stream never
        # connects. Accepting whatever the device offers is correct here: the
        # image type is already pinned via PixelFormat at open time.
        try:
            sink.alloc_and_queue_buffers(
                max(int(min_buffers_required), self.EXTRA_BUFFERS)
            )
        except Exception:
            # Not fatal: the sink falls back to allocating the minimum itself.
            logger.warning(
                "Could not pre-allocate IC4 buffers; the sink will allocate the "
                "minimum, which leaves no headroom if a consumer stalls.",
                exc_info=True,
            )
        return True

    def frames_queued(self, sink) -> None:
        # Drain the sink fully: the SDK may deliver several buffers per
        # notification, and anything left behind is a leaked frame.
        #
        # This runs on the SDK's stream thread. Letting an exception escape into
        # native code is undefined behaviour, so everything is handled here.
        while True:
            try:
                buffer = sink.try_pop_output_buffer()
            except Exception:
                logger.exception("Unexpected error draining the IC4 sink")
                return
            if buffer is None:
                return  # output queue empty
            try:
                # numpy_copy, NOT numpy_wrap: the wrap is a view onto a buffer
                # the SDK recycles the moment we return.
                self._queue.push(normalize_frame(buffer.numpy_copy()))
            except Exception:
                logger.exception("Could not copy an IC4 frame out of its buffer")
                return
            finally:
                # Documented contract: a popped buffer returns to the sink's
                # free queue when released or deleted. Explicit beats relying on
                # refcount timing in a hot acquisition loop.
                try:
                    buffer.release()
                except Exception:
                    pass


class MockIC4Camera:
    """Headless stand-in for :class:`IC4Camera`.

    Frame production is *trigger-gated* — frames appear only when a software or
    hardware trigger has been fired, and every frame is distinct. Both properties
    matter: a mock that emits frames freely, or emits identical ones, would pass
    tests that the original bug should fail.
    """

    def __init__(
        self,
        serial=None,
        max_queued_frames: int = 256,
        pixel_format=None,
        reset_to_default: bool = True,
    ):
        self._serial = serial if serial is not None else "MOCK_TIS_33UX250"
        self._model = "Mock TIS DMK 33UX250 (IC4)"

        self._sensor_width = 2448
        self._sensor_height = 2048
        self._roi = (0, 0, self._sensor_width, self._sensor_height)

        # 'Continuous', not 'Off', because that is what loading the Default user
        # set leaves behind on the real camera — the mock has to start in the
        # broken state or it cannot show the fix working.
        self._exposure_auto = "Continuous"
        self._gain_auto = "Continuous"
        self._exposure_us = DEFAULT_EXPOSURE_US
        self._gain = 0.0
        self._ensure_manual_exposure_and_gain()
        self._trigger_enabled = False
        self._trigger_source = None
        self._trigger_activation = None
        self._streaming = False
        self._frame_count = 0
        self._dtype = dtype_for_pixel_format(pixel_format)

        self._queue = FrameQueue(maxlen=max_queued_frames, logger=logger)
        logger.info(f"Initialized mock IC4 camera: {self._serial}")

    @property
    def serial(self):
        return self._serial

    @property
    def model(self):
        return self._model

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    @property
    def dtype(self) -> np.dtype:
        return self._dtype

    @property
    def sensor_width_pixels(self) -> int:
        return self._sensor_width

    @property
    def sensor_height_pixels(self) -> int:
        return self._sensor_height

    @property
    def image_width_pixels(self) -> int:
        return self._roi[2]

    @property
    def image_height_pixels(self) -> int:
        return self._roi[3]

    #: What the DMK 33UX250's auto algorithm settles on: 1/30 s, gain railed.
    #: Reproduced so a test can assert the *observed* failure, not a stand-in.
    AUTO_EXPOSURE_US = 33333.0
    AUTO_GAIN_DB = 48.0

    def _ensure_manual_exposure_and_gain(self) -> None:
        self._exposure_auto = "Off"
        self._gain_auto = "Off"

    def set_exposure_us(self, exposure_us) -> float:
        self._ensure_manual_exposure_and_gain()
        self._exposure_us = float(exposure_us)
        return self.get_exposure_us()

    def get_exposure_us(self) -> float:
        # Mirrors the device: while the auto algorithm owns the property, what
        # was written to it is irrelevant. A mock that echoed the written value
        # back would pass every test the hardware fails.
        if self._exposure_auto != "Off":
            return self.AUTO_EXPOSURE_US
        return self._exposure_us

    def set_gain(self, gain) -> float:
        self._ensure_manual_exposure_and_gain()
        self._gain = float(gain)
        return self.get_gain()

    def get_gain(self) -> float:
        if self._gain_auto != "Off":
            return self.AUTO_GAIN_DB
        return self._gain

    #: Width/Height/Offset increment, as GenICam sensors impose (commonly 4 or
    #: 8 px). The mock enforces it for the same reason it starts with the autos
    #: on: a mock that took every ROI verbatim could never show a caller
    #: describing frames the camera is not producing.
    ROI_INCREMENT = 4

    def set_roi(self, x0, y0, width, height) -> tuple:
        def fit(value, limit):
            value = max(0, min(int(value), limit))
            return value - value % self.ROI_INCREMENT

        self._roi = (
            fit(x0, self._sensor_width), fit(y0, self._sensor_height),
            fit(width, self._sensor_width), fit(height, self._sensor_height),
        )
        return self._roi

    def set_trigger_enabled(self, enabled: bool, source=None, activation=None) -> None:
        self._trigger_enabled = bool(enabled)
        if enabled and source is not None:
            self._trigger_source = source
        if enabled and activation is not None:
            self._trigger_activation = activation

    def is_trigger_enabled(self) -> bool:
        return self._trigger_enabled

    def execute_software_trigger(self) -> None:
        self._emit_frame()

    def simulate_hardware_trigger(self, n: int = 1) -> None:
        """Test helper: fire ``n`` external triggers."""
        for _ in range(n):
            self._emit_frame()

    def simulate_auto_reenabled(self) -> None:
        """Test helper: put the autos back on, as the vendor GUI does.

        IC Capture can be opened alongside ImSwitch and will happily re-tick the
        auto checkboxes on a camera ImSwitch has open, so recovering from this
        state is a runtime requirement, not just an open-time one.
        """
        self._exposure_auto = "Continuous"
        self._gain_auto = "Continuous"

    def _emit_frame(self) -> None:
        """Produce one distinct frame, exactly as a real trigger would."""
        if not self._streaming:
            return
        h, w = self.image_height_pixels, self.image_width_pixels
        self._frame_count += 1
        # A per-frame offset makes every frame distinct, so a test asserting
        # "N triggers produce N *different* frames" is meaningful. Frame count
        # alone would have looked healthy under the original duplicate bug.
        offset = (self._frame_count * 97) % 4096
        y = np.linspace(0, 1024, h, dtype=np.float32)[:, None]
        x = np.linspace(0, 1024, w, dtype=np.float32)[None, :]
        raw = ((y + x) / 2 + offset).astype(self._dtype)
        # Mimic ImageBuffer.numpy_copy(), which returns (H, W, 1) for mono
        # formats, and push it through the same normalization the real listener
        # uses. A mock that emitted a clean (H, W) would silently pass tests the
        # hardware fails — which is exactly what happened before the rig probe.
        self._queue.push(normalize_frame(raw[:, :, None]))

    def start_stream(self) -> None:
        self._streaming = True

    def stop_stream(self) -> None:
        self._streaming = False

    def pop_frames(self) -> list:
        return self._queue.drain()

    def latest_frame(self):
        return self._queue.latest()

    def flush(self) -> None:
        self._queue.clear()

    @property
    def queued_frame_count(self) -> int:
        return len(self._queue)

    @property
    def dropped_frame_count(self) -> int:
        return self._queue.dropped

    def dispose(self) -> None:
        self._streaming = False
        self._queue.clear()
        logger.info("Mock IC4 camera disposed")
