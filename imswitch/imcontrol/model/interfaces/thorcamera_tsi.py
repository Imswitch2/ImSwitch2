"""Thorlabs Scientific Camera SDK (TSI) interface.

Minimal wrapper around TLCameraSDK for Zelux/Kiralux/Quantalux cameras.
Provides MockThorTSICamera for headless operation.
"""
import logging
import numpy as np

logger = logging.getLogger(__name__)


#: Frames the SDK ring buffer holds between the camera and the drain. What the
#: depth buys is tolerance to latency spikes -- a GC pause, an HDF5 resize, a
#: writer-backpressure block -- of up to depth/framerate seconds; a depth of
#: one drops a frame on every such spike.
DEFAULT_FRAME_BUFFER_DEPTH = 4


class ThorTSICamera:
    """Wrapper around Thorlabs TLCameraSDK.
    
    Args:
        serial: Camera serial number. If None, opens the first available camera.
        dll_directory: Path to ThorCam DLLs. If provided, adds to PATH/DLL search.
    """
    
    def __init__(self, serial=None, dll_directory=None):
        if dll_directory is not None:
            self._configure_dll_path(dll_directory)
        
        try:
            from thorlabs_tsi_sdk.tl_camera import TLCameraSDK
        except ImportError as e:
            raise ImportError(
                "thorlabs_tsi_sdk not found. Install with: "
                "pip install thorlabs_tsi_sdk or pip install ImSwitch[hardware]"
            ) from e
        
        self._sdk = TLCameraSDK()
        available = self._sdk.discover_available_cameras()
        
        if len(available) == 0:
            raise RuntimeError("No Thorlabs TSI cameras found")
        
        target_serial = serial if serial is not None else available[0]
        if target_serial not in available:
            raise ValueError(
                f"Camera {target_serial} not found. Available: {available}"
            )
        
        self._camera = self._sdk.open_camera(target_serial)
        self._serial = target_serial
        logger.info(f"Opened Thorlabs TSI camera: {target_serial}")
        
        # Default configuration
        self._camera.frames_per_trigger_zero_for_unlimited = 1
        self._camera.operation_mode = 0  # SOFTWARE_TRIGGER
    
    @staticmethod
    def _configure_dll_path(dll_directory):
        """Add DLL directory to Windows search path."""
        import os
        import sys
        
        if sys.platform == "win32":
            abs_path = os.path.abspath(dll_directory)
            os.environ["PATH"] = abs_path + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(abs_path)
                logger.debug(f"Added DLL directory: {abs_path}")
            except (AttributeError, OSError) as e:
                logger.warning(f"Failed to add DLL directory: {e}")
    
    @property
    def serial(self):
        """Camera serial number."""
        return self._serial
    
    @property
    def model(self):
        """Camera model string."""
        return self._camera.model
    
    @property
    def is_armed(self):
        """Whether the camera is currently armed for acquisition."""
        return bool(getattr(self._camera, 'is_armed', False))
    
    @property
    def sensor_width_pixels(self):
        """Full sensor width in pixels."""
        return self._camera.sensor_width_pixels
    
    @property
    def sensor_height_pixels(self):
        """Full sensor height in pixels."""
        return self._camera.sensor_height_pixels
    
    @property
    def image_width_pixels(self):
        """Current ROI width in pixels."""
        return self._camera.image_width_pixels
    
    @property
    def image_height_pixels(self):
        """Current ROI height in pixels."""
        return self._camera.image_height_pixels
    
    @property
    def roi(self):
        """Current ROI as (x0, y0, x1, y1) tuple."""
        return self._camera.roi
    
    def set_roi(self, x0, y0, x1, y1):
        """Set region of interest (pixel coordinates).
        
        Args:
            x0: Upper-left X coordinate
            y0: Upper-left Y coordinate
            x1: Lower-right X coordinate (inclusive)
            y1: Lower-right Y coordinate (inclusive)
        """
        self._camera.roi = (x0, y0, x1, y1)
        logger.debug(f"Set ROI: ({x0}, {y0}, {x1}, {y1})")
    
    def set_exposure_us(self, exposure_us):
        """Set exposure time in microseconds."""
        self._camera.exposure_time_us = int(exposure_us)
        logger.debug(f"Set exposure: {exposure_us} µs")
    
    def get_exposure_us(self):
        """Get current exposure time in microseconds."""
        return self._camera.exposure_time_us
    
    def set_gain(self, gain):
        """Set camera gain (dB or camera-specific units)."""
        self._camera.gain = int(gain)
        logger.debug(f"Set gain: {gain}")
    
    def get_gain(self):
        """Get current gain value."""
        return self._camera.gain
    
    def set_frame_rate(self, hz):
        """Set frame rate in Hz and enable frame rate control."""
        self._camera.frame_rate_control_value = float(hz)
        self._camera.is_frame_rate_control_enabled = True
        logger.debug(f"Set frame rate: {hz} Hz")
    
    def set_frame_rate_enabled(self, enabled):
        """Enable or disable frame rate control."""
        self._camera.is_frame_rate_control_enabled = bool(enabled)
    
    def set_trigger_mode(self, mode):
        """Set trigger/operation mode.

        The Thorlabs SDK rejects ``operation_mode`` writes while the camera is
        armed (error 1004, "Invalid operation"). We disarm transparently and
        re-arm with the previous buffer size so callers can change mode at
        runtime from the GUI.

        Args:
            mode: One of 'software', 'hardware', 'bulb'
        """
        mode_map = {
            'software': 0,
            'hardware': 1,
            'bulb': 2
        }
        if mode not in mode_map:
            raise ValueError(f"Invalid mode '{mode}'. Use: {list(mode_map.keys())}")

        was_armed = bool(getattr(self._camera, 'is_armed', False))
        # The depth this wrapper armed with. It used to be read back from
        # frames_per_trigger_zero_for_unlimited, a different quantity that
        # happens to be a small integer too: __init__ sets it to 1, so the
        # first Operation Mode change -- including the one every startup makes
        # while restoring the saved detector state -- shrank the SDK ring from
        # four frames to one for the rest of the session.
        buffer_size = getattr(self, '_armed_buffer_size', None) or DEFAULT_FRAME_BUFFER_DEPTH
        if was_armed:
            self._camera.disarm()

        self._camera.operation_mode = mode_map[mode]
        logger.debug(f"Set trigger mode: {mode}")

        if was_armed:
            self._camera.arm(buffer_size)
    
    def set_trigger_polarity(self, polarity):
        """Set trigger polarity.
        
        Args:
            polarity: 'active_high' (rising edge) or 'active_low' (falling edge)
        """
        polarity_map = {
            'active_high': 0,
            'active_low': 1
        }
        if polarity not in polarity_map:
            raise ValueError(
                f"Invalid polarity '{polarity}'. Use: {list(polarity_map.keys())}"
            )
        
        self._camera.trigger_polarity = polarity_map[polarity]
        logger.debug(f"Set trigger polarity: {polarity}")
    
    def arm(self, buffer_size=DEFAULT_FRAME_BUFFER_DEPTH):
        """Arm the camera for acquisition.
        
        Args:
            buffer_size: Number of frames to buffer internally
        """
        if not self._camera.is_armed:
            self._camera.arm(buffer_size)
            self._armed_buffer_size = int(buffer_size)
            logger.debug(f"Armed camera with buffer size {buffer_size}")
    
    def disarm(self):
        """Disarm the camera."""
        if self._camera.is_armed:
            self._camera.disarm()
            logger.debug("Disarmed camera")
    
    def issue_software_trigger(self):
        """Issue a software trigger (only in software trigger mode)."""
        self._camera.issue_software_trigger()
    
    def get_pending_frame(self):
        """Poll for a pending frame.
        
        Returns:
            ndarray (H, W) uint16 frame, or None if no frame available
        """
        frame = self._camera.get_pending_frame_or_null()
        if frame is None:
            return None
        
        image = np.copy(frame.image_buffer).reshape(
            self.image_height_pixels,
            self.image_width_pixels
        )
        return image
    
    def dispose(self):
        """Close camera and dispose SDK resources."""
        if self._camera is not None:
            self.disarm()
            self._camera.dispose()
            self._camera = None
        
        if self._sdk is not None:
            self._sdk.dispose()
            self._sdk = None
        
        logger.info("Disposed Thorlabs TSI camera")


class MockThorTSICamera:
    """Mock Thorlabs TSI camera for headless testing."""
    
    def __init__(self, serial=None, dll_directory=None):
        self._serial = serial if serial is not None else "MOCK_TSI_12345"
        self._model = "Mock Thorlabs TSI Camera"
        
        self._sensor_width = 2448
        self._sensor_height = 2048
        self._roi = (0, 0, self._sensor_width - 1, self._sensor_height - 1)
        
        self._exposure_us = 50000
        self._gain = 0
        self._frame_rate = 30.0
        self._frame_rate_enabled = True
        self._trigger_mode = 0  # software
        self._trigger_polarity = 0  # active_high
        
        self._armed = False
        self._frame_count = 0
        
        # Trigger-gated frame production (Phase 0a)
        self._pending_software_triggers = 0
        self._pending_hardware_triggers = 0
        
        logger.info(f"Initialized mock Thorlabs TSI camera: {self._serial}")
    
    @property
    def serial(self):
        return self._serial
    
    @property
    def model(self):
        return self._model
    
    @property
    def is_armed(self):
        """Whether the camera is currently armed for acquisition."""
        return self._armed
    
    @property
    def sensor_width_pixels(self):
        return self._sensor_width
    
    @property
    def sensor_height_pixels(self):
        return self._sensor_height
    
    @property
    def image_width_pixels(self):
        return self._roi[2] - self._roi[0] + 1
    
    @property
    def image_height_pixels(self):
        return self._roi[3] - self._roi[1] + 1
    
    @property
    def roi(self):
        return self._roi
    
    def set_roi(self, x0, y0, x1, y1):
        self._roi = (x0, y0, x1, y1)
        logger.debug(f"Mock: Set ROI: ({x0}, {y0}, {x1}, {y1})")
    
    def set_exposure_us(self, exposure_us):
        self._exposure_us = int(exposure_us)
        logger.debug(f"Mock: Set exposure: {exposure_us} µs")
    
    def get_exposure_us(self):
        return self._exposure_us
    
    def set_gain(self, gain):
        self._gain = int(gain)
        logger.debug(f"Mock: Set gain: {gain}")
    
    def get_gain(self):
        return self._gain
    
    def set_frame_rate(self, hz):
        self._frame_rate = float(hz)
        self._frame_rate_enabled = True
        logger.debug(f"Mock: Set frame rate: {hz} Hz")
    
    def set_frame_rate_enabled(self, enabled):
        self._frame_rate_enabled = bool(enabled)
    
    def set_trigger_mode(self, mode):
        mode_map = {'software': 0, 'hardware': 1, 'bulb': 2}
        if mode not in mode_map:
            raise ValueError(f"Invalid mode '{mode}'")
        self._trigger_mode = mode_map[mode]
        logger.debug(f"Mock: Set trigger mode: {mode}")
    
    def set_trigger_polarity(self, polarity):
        polarity_map = {'active_high': 0, 'active_low': 1}
        if polarity not in polarity_map:
            raise ValueError(f"Invalid polarity '{polarity}'")
        self._trigger_polarity = polarity_map[polarity]
        logger.debug(f"Mock: Set trigger polarity: {polarity}")
    
    def arm(self, buffer_size=2):
        self._armed = True
        self.armed_buffer_size = int(buffer_size)
        logger.debug(f"Mock: Armed with buffer size {buffer_size}")
    
    def disarm(self):
        self._armed = False
        logger.debug("Mock: Disarmed")
    
    def issue_software_trigger(self):
        self._pending_software_triggers += 1
        logger.debug("Mock: Software trigger issued")
    
    def simulate_hardware_trigger(self, n: int = 1):
        """Test helper: queue n hardware triggers."""
        self._pending_hardware_triggers += n
        logger.debug(f"Mock: Simulated {n} hardware trigger(s)")
    
    def get_pending_frame(self):
        """Return a synthetic uint16 frame if a trigger is pending, else None.
        
        In software mode: consume a pending software trigger.
        In hardware/bulb mode: consume a pending hardware trigger.
        """
        if not self._armed:
            return None
        
        # Check trigger availability based on mode
        if self._trigger_mode == 0:  # software
            if self._pending_software_triggers <= 0:
                return None
            self._pending_software_triggers -= 1
        else:  # hardware (1) or bulb (2)
            if self._pending_hardware_triggers <= 0:
                return None
            self._pending_hardware_triggers -= 1
        
        h = self.image_height_pixels
        w = self.image_width_pixels
        
        # Generate gradient pattern based on frame count
        self._frame_count += 1
        offset = (self._frame_count * 100) % 32768
        
        y_grad = np.linspace(0, 16384, h, dtype=np.float32)[:, None]
        x_grad = np.linspace(0, 16384, w, dtype=np.float32)[None, :]
        frame = ((y_grad + x_grad) / 2 + offset).astype(np.uint16)
        
        return frame
    
    def dispose(self):
        self._armed = False
        logger.info("Mock: Disposed")
