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

logger = logging.getLogger(__name__)

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
            # try_set_value: not every model supports user sets.
            self._pm.try_set_value(ic4.PropId.USER_SET_SELECTOR, "Default")
            self._pm.try_set_value(ic4.PropId.USER_SET_LOAD, 1)

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

    def set_exposure_us(self, exposure_us) -> None:
        self._pm.set_value(self._ic4.PropId.EXPOSURE_TIME, float(exposure_us))

    def get_exposure_us(self) -> float:
        return float(self._pm.get_value_float(self._ic4.PropId.EXPOSURE_TIME))

    def set_gain(self, gain) -> None:
        self._pm.set_value(self._ic4.PropId.GAIN, float(gain))

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

    def set_roi(self, x0, y0, width, height) -> None:
        """Set the readout region. Requires the stream to be stopped."""
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

    # -- trigger ----------------------------------------------------------

    def set_trigger_enabled(self, enabled: bool, source=None) -> None:
        """Arm or disarm the hardware trigger in software.

        The legacy path could not do this — ``pyicic``'s ``enable_trigger`` was
        called once with ``False`` at init and swallowed its own error return, so
        arming was a manual step in the vendor's properties dialog.
        """
        pm, pid = self._pm, self._ic4.PropId
        # TriggerSelector must be set before TriggerMode on models that expose
        # it; try_set_value tolerates those that do not.
        pm.try_set_value(pid.TRIGGER_SELECTOR, "FrameStart")
        pm.set_value(pid.TRIGGER_MODE, "On" if enabled else "Off")
        if enabled and source is not None:
            pm.set_value(pid.TRIGGER_SOURCE, str(source))

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

    def sink_connected(self, sink, image_type, min_buffers_required) -> bool:
        # Required by the SDK and must return True, or the stream never
        # connects. Accepting whatever the device offers is correct here: the
        # image type is already pinned via PixelFormat at open time.
        return True

    def frames_queued(self, sink) -> None:
        # Drain the sink fully: the SDK may deliver several buffers per
        # notification, and anything left behind is a leaked frame.
        #
        # This runs on the SDK's stream thread. Letting an exception escape into
        # native code is undefined behaviour, so everything is handled here.
        while True:
            try:
                buffer = sink.pop_output_buffer()
            except Exception as e:
                # pop_output_buffer raises rather than returning None once the
                # output queue is empty, which is the documented way to discover
                # there is nothing left. Anything else is a real error and must
                # not be silently swallowed.
                if type(e).__name__ != "IC4Exception":
                    logger.exception("Unexpected error draining the IC4 sink")
                return
            try:
                # numpy_copy, NOT numpy_wrap: the wrap is a view onto a buffer
                # the SDK recycles the moment we return.
                self._queue.push(buffer.numpy_copy())
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

        self._exposure_us = DEFAULT_EXPOSURE_US
        self._gain = 0.0
        self._trigger_enabled = False
        self._trigger_source = None
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

    def set_exposure_us(self, exposure_us) -> None:
        self._exposure_us = float(exposure_us)

    def get_exposure_us(self) -> float:
        return self._exposure_us

    def set_gain(self, gain) -> None:
        self._gain = float(gain)

    def get_gain(self) -> float:
        return self._gain

    def set_roi(self, x0, y0, width, height) -> None:
        self._roi = (int(x0), int(y0), int(width), int(height))

    def set_trigger_enabled(self, enabled: bool, source=None) -> None:
        self._trigger_enabled = bool(enabled)
        if enabled and source is not None:
            self._trigger_source = source

    def is_trigger_enabled(self) -> bool:
        return self._trigger_enabled

    def execute_software_trigger(self) -> None:
        self._emit_frame()

    def simulate_hardware_trigger(self, n: int = 1) -> None:
        """Test helper: fire ``n`` external triggers."""
        for _ in range(n):
            self._emit_frame()

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
        self._queue.push(((y + x) / 2 + offset).astype(self._dtype))

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
