"""DetectorManager for Thorlabs Scientific Cameras (TSI SDK).

Supports Zelux, Kiralux, and Quantalux cameras via thorlabs_tsi_sdk.
"""
from imswitch.imcommon.model import initLogger
from .DetectorManager import (
    DetectorManager, DetectorNumberParameter, DetectorListParameter
)


class ThorCamTSIManager(DetectorManager):
    """DetectorManager for Thorlabs scientific cameras (TSI SDK).
    
    Manager properties:
    
    - ``cameraSerial`` -- camera serial number (str or null). If null, opens
      the first available camera. Set to a string starting with "MOCK_" to
      load a mock camera for headless testing.
    - ``dllLocation`` -- path to ThorCam DLLs (Windows only). Default: "dlls/64_lib"
    - ``cameraPixelSizeUm`` -- optically effective (sample-plane) pixel size in
      micrometers, i.e. physical sensor pitch divided by total optical
      magnification. Used by downstream modules such as tiling and stitching.
      Exposed at runtime as the ``'Camera pixel size'`` detector parameter.
      Default: 0.15 µm.
    - ``defaults`` -- dict of default values:
        - ``exposure_us``: exposure time in microseconds (default: 50000)
        - ``gain``: camera gain (default: 0)
        - ``operation_mode``: 'Software', 'Hardware', or 'Bulb' (default: 'Software')
        - ``trigger_polarity``: 'Active High' or 'Active Low' (default: 'Active High')
        - ``frame_rate``: frame rate in Hz when frame rate control enabled (default: 30)
    """
    
    def __init__(self, detectorInfo, name, **_lowLevelManagers):
        self.__logger = initLogger(self, instanceName=name)
        
        # Extract manager properties
        props = detectorInfo.managerProperties
        serial = props.get('cameraSerial', None)
        dll_location = props.get('dllLocation', 'dlls/64_lib')
        defaults = props.get('defaults', {})
        self._flushFrameLimit = int(props.get('flushFrameLimit', 256))
        from imswitch.imcontrol.model.interfaces.thorcamera_tsi import DEFAULT_FRAME_BUFFER_DEPTH
        self._frameBufferDepth = int(props.get('frameBufferDepth', DEFAULT_FRAME_BUFFER_DEPTH))
        
        # Get default values
        default_exposure_us = defaults.get('exposure_us', 50000)
        default_gain = defaults.get('gain', 0)
        default_mode = defaults.get('operation_mode', 'Software')
        default_polarity = defaults.get('trigger_polarity', 'Active High')
        default_frame_rate = defaults.get('frame_rate', 30)
        
        # Initialize camera (with mock fallback)
        self._camera = self._initCamera(serial, dll_location)
        
        # Get sensor dimensions
        fullShape = (
            self._camera.sensor_width_pixels,
            self._camera.sensor_height_pixels
        )
        
        model = self._camera.model
        
        # Prepare parameters
        parameters = {
            'Exposure': DetectorNumberParameter(
                group='Acquisition', value=default_exposure_us,
                valueUnits='µs', editable=True
            ),
            'Gain': DetectorNumberParameter(
                group='Acquisition', value=default_gain,
                valueUnits='dB', editable=True
            ),
            'Frame Rate': DetectorNumberParameter(
                group='Acquisition', value=default_frame_rate,
                valueUnits='Hz', editable=True
            ),
            'Operation Mode': DetectorListParameter(
                group='Trigger', value=default_mode,
                options=['Software', 'Hardware', 'Bulb'],
                editable=True
            ),
            'Trigger Polarity': DetectorListParameter(
                group='Trigger', value=default_polarity,
                options=['Active High', 'Active Low'],
                editable=True
            ),
            'ROI X0': DetectorNumberParameter(
                group='ROI', value=0, valueUnits='px', editable=True
            ),
            'ROI Y0': DetectorNumberParameter(
                group='ROI', value=0, valueUnits='px', editable=True
            ),
            'ROI X1': DetectorNumberParameter(
                group='ROI', value=fullShape[0] - 1,
                valueUnits='px', editable=True
            ),
            'ROI Y1': DetectorNumberParameter(
                group='ROI', value=fullShape[1] - 1,
                valueUnits='px', editable=True
            ),
            # Effective (sample-plane) pixel size — see base-class helper.
            'Camera pixel size': DetectorManager.makeCameraPixelSizeParameter(
                detectorInfo
            ),
        }
        
        super().__init__(
            detectorInfo, name, fullShape=fullShape,
            supportedBinnings=[1],
            model=model, parameters=parameters, croppable=True
        )
        
        # Apply defaults
        self._applyDefaults()
        
        # Arm camera for continuous acquisition
        self._camera.arm(buffer_size=self._frameBufferDepth)
        self.__logger.info(f"Initialized {model}, serial: {self._camera.serial}")
    
    def _initCamera(self, serial, dll_location):
        """Initialize camera with fallback to mock."""
        # Mock fallback: serial starts with "MOCK_" or import fails
        if serial is not None:
            serial = str(serial)
        use_mock = serial is not None and serial.startswith("MOCK_")
        
        if not use_mock:
            try:
                from imswitch.imcontrol.model.interfaces.thorcamera_tsi import (
                    ThorTSICamera
                )
                camera = ThorTSICamera(serial=serial, dll_directory=dll_location)
                return camera
            except (ImportError, RuntimeError, ValueError) as e:
                self.__logger.warning(
                    f"Failed to initialize Thorlabs TSI camera: {e}. "
                    f"Loading mock camera."
                )
                use_mock = True
        
        # Load mock
        from imswitch.imcontrol.model.interfaces.thorcamera_tsi import (
            MockThorTSICamera
        )
        return MockThorTSICamera(serial=serial)
    
    def _applyDefaults(self):
        """Apply default parameter values to hardware."""
        self.setParameter('Exposure', self.parameters['Exposure'].value)
        self.setParameter('Gain', self.parameters['Gain'].value)
        self.setParameter('Operation Mode', self.parameters['Operation Mode'].value)
        self.setParameter('Trigger Polarity', self.parameters['Trigger Polarity'].value)
        self.setParameter('Frame Rate', self.parameters['Frame Rate'].value)
    
    def getLatestFrame(self, is_save=False):
        """Get the latest frame from camera.
        
        In software trigger mode, issues a software trigger and polls for frame.
        In hardware trigger mode, polls for pending frame.
        """
        # Issue software trigger if in software mode
        if self.parameters['Operation Mode'].value == 'Software':
            self._camera.issue_software_trigger()
        
        # Poll for frame (with simple retry for software trigger)
        import time
        for _ in range(10):  # Max 10 retries
            frame = self._camera.get_pending_frame()
            if frame is not None:
                return frame
            time.sleep(0.001)  # 1ms between polls
        
        # No frame available, return zeros
        self.__logger.warning("No frame available, returning zeros")
        return self._getEmptyFrame()
    
    def _getEmptyFrame(self):
        """Return a zero-filled frame matching current ROI size."""
        import numpy as np
        return np.zeros(
            (self._camera.image_height_pixels, self._camera.image_width_pixels),
            dtype=np.uint16
        )
    
    def setParameter(self, name, value):
        """Set a parameter value and update hardware."""
        super().setParameter(name, value)
        
        if name == 'Exposure':
            self._camera.set_exposure_us(value)
        
        elif name == 'Gain':
            self._camera.set_gain(value)
        
        elif name == 'Frame Rate':
            self._camera.set_frame_rate(value)
        
        elif name == 'Operation Mode':
            mode_map = {
                'Software': 'software',
                'Hardware': 'hardware',
                'Bulb': 'bulb'
            }
            self._camera.set_trigger_mode(mode_map[value])
        
        elif name == 'Trigger Polarity':
            polarity_map = {
                'Active High': 'active_high',
                'Active Low': 'active_low'
            }
            self._camera.set_trigger_polarity(polarity_map[value])
        
        elif name in ['ROI X0', 'ROI Y0', 'ROI X1', 'ROI Y1']:
            # Update ROI when any corner changes
            self._updateROI()
        
        return self.parameters
    
    def _updateROI(self):
        """Apply current ROI parameters to camera."""
        x0 = int(self.parameters['ROI X0'].value)
        y0 = int(self.parameters['ROI Y0'].value)
        x1 = int(self.parameters['ROI X1'].value)
        y1 = int(self.parameters['ROI Y1'].value)
        
        # Validate and clamp to sensor bounds
        x0 = max(0, min(x0, self._camera.sensor_width_pixels - 1))
        y0 = max(0, min(y0, self._camera.sensor_height_pixels - 1))
        x1 = max(x0, min(x1, self._camera.sensor_width_pixels - 1))
        y1 = max(y0, min(y1, self._camera.sensor_height_pixels - 1))
        
        # Re-arm required for ROI change
        was_armed = True  # Assume armed since we arm in __init__
        if was_armed:
            self._camera.disarm()
        
        self._camera.set_roi(x0, y0, x1, y1)
        
        if was_armed:
            self._camera.arm(buffer_size=self._frameBufferDepth)
        
        # Update shape
        self._shape = (x1 - x0 + 1, y1 - y0 + 1)
        self._frameStart = (x0, y0)
    
    def getChunk(self):
        """Get a chunk of frames for recording.
        
        Returns a 3-D (numFrames, height, width) array as per the
        DetectorManager.getChunk() contract (used by readChunk() for
        multi-consumer recording distribution). NEVER delegates to
        getLatestFrame() because that method fabricates zero frames for
        live-view display, which would corrupt recordings.
        
        - Software mode: issue one software trigger, poll briefly, return (1, H, W)
          or empty chunk if frame doesn't arrive.
        - Hardware/Bulb modes: drain all pending frames, return (n, H, W) or empty
          chunk. Do NOT issue triggers or fabricate.
        
        See DetectorManager.readChunk() implementation (list.extend requires 3-D).
        """
        import numpy as np
        import time
        
        mode = self.parameters['Operation Mode'].value
        h = self._camera.image_height_pixels
        w = self._camera.image_width_pixels
        dtype = np.uint16
        
        if mode == 'Software':
            # Issue one software trigger and poll for the resulting frame
            self._camera.issue_software_trigger()
            
            for _ in range(10):  # Max 10 retries (~10ms)
                frame = self._camera.get_pending_frame()
                if frame is not None:
                    # Return as (1, H, W)
                    return frame[np.newaxis, :, :]
                time.sleep(0.001)
            
            # No frame arrived, return empty chunk (never fabricate)
            return np.empty((0, h, w), dtype=dtype)
        
        else:  # Hardware or Bulb mode
            # Drain all currently pending frames (do NOT issue trigger)
            frames = []
            while True:
                frame = self._camera.get_pending_frame()
                if frame is None:
                    break
                frames.append(frame)
            
            if len(frames) == 0:
                return np.empty((0, h, w), dtype=dtype)
            else:
                # Stack to (n, H, W)
                return np.stack(frames, axis=0)
    
    def flushBuffers(self):
        """Flush internal buffers by polling all pending frames."""
        frames_flushed = 0
        while self._camera.get_pending_frame() is not None:
            frames_flushed += 1
            if frames_flushed >= self._flushFrameLimit:
                self.__logger.warning(
                    f'Stopped ThorCam buffer flush after {frames_flushed} frames; '
                    'camera still reports pending frames.'
                )
                break
    
    def crop(self, hpos, vpos, hsize, vsize):
        """Crop the detector readout region.
        
        Args:
            hpos: Horizontal position (x0)
            vpos: Vertical position (y0)
            hsize: Horizontal size (width)
            vsize: Vertical size (height)
        """
        x0 = hpos
        y0 = vpos
        x1 = hpos + hsize - 1
        y1 = vpos + vsize - 1
        
        # Update parameters and apply
        self.parameters['ROI X0'].value = x0
        self.parameters['ROI Y0'].value = y0
        self.parameters['ROI X1'].value = x1
        self.parameters['ROI Y1'].value = y1
        
        self._updateROI()
    
    def startAcquisition(self):
        """Ensure the camera is armed for acquisition.

        Idempotent: arms only if not already armed, so recording can recover if the
        camera was disarmed after __init__ (SDK error, external disarm, mode change).
        Other camera managers actively (re)start acquisition here; this one keeps the
        camera continuously armed but must not silently no-op when it is disarmed.
        """
        if not self._camera.is_armed:
            self._camera.arm(buffer_size=self._frameBufferDepth)
    
    def stopAcquisition(self):
        """Stop acquisition (no-op, camera stays armed)."""
        pass
    
    def stopAcquisitionForROIChange(self):
        """Temporarily stop for ROI change (handled in _updateROI)."""
        pass
    
    def finalize(self):
        """Cleanup: disarm and dispose camera."""
        super().finalize()
        self.__logger.debug("Finalizing ThorCam TSI manager...")
        if self._camera is not None:
            self._camera.dispose()
            self._camera = None
    
